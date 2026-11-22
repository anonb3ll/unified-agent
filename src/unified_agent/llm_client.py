"""Shared LLM client for the nightly sleep pass (Roadmap Phase 1.1).

One fallback chain, several consumers (System Leverage Plan "convergence"
note): the sleep pass's digest and contradiction adjudication, session
capture, and the Slack/Zulip chat replies. Chain order (operator rule
2026-10-13, task #2025): automations run on subscriptions or OpenRouter keys,
preferring headless Claude sessions, and no Google AI Studio API key sits in an
automation path. So:

1. ``claude_cli`` — the Claude Code CLI run headless (``claude -p``) on the
   operator's logged-in claude.ai subscription. Before any completion,
   ``claude auth status`` must show a claude.ai login (a Console or API-key
   login fails closed), ``claude --help`` must list ``--safe-mode`` and
   ``--permission-prompts`` (an older CLI fails closed as "unsupported CLI
   version"), and the child gets an allowlisted environment with no
   ``ANTHROPIC_*`` variable. It runs in safe mode with no tools, MCP servers,
   hooks, skills or settings, in an empty private working directory, so it is
   a plain completion and never an agent. Never ``--bare``: that mode reads
   only ``ANTHROPIC_API_KEY`` and never the subscription's OAuth credentials.
2. ``openrouter`` — the operator's OpenRouter key, on a pinned model
   (``UA_OPENROUTER_MODEL``, default ``deepseek/deepseek-v4.1-flash``).

No local model is in the chain (operator decision, 2026-10-13): the local 1.5B
judge produced 84 false contradictions out of 132 on 2026-10-01.
``_call_ollama`` remains for explicit callers and tests; no chain selects it.
The Google AI Studio leg is out too, and `complete()` has no way to select it:
Gemini is reachable only through the explicit ``gemini_ask`` tool.
``_call_ai_studio`` remains as the thin helper over ``gemini_ask`` (kept with
its failure classification and tests) and is not wired anywhere. Every
provider degrades to None on any failure — never raises — so `complete()`
alone decides whether the whole chain failed.

An explicitly enabled subscription-pool endpoint (task #1154 — a self-hosted
OpenAI-compatible proxy in front of CLI-subscription auth, e.g. CLIProxyAPI)
is an optional leg inserted immediately before openrouter, only when both the
global (`UA_SUBSCRIPTION_POOL_ENABLED=1`) and per-call opt-ins are set. It
stays off by default: a fresh install with neither opt-in set gets exactly
the ``claude_cli -> openrouter`` chain. See
docs/llm-subscriptions-leg-runbook.md for setup and the ToS-risk
acknowledgment — wrapping subscription auth into an API may violate provider
terms, the official CLIs remain the primary dev lanes, and the product never
depends on this leg. The ``claude_cli`` leg is that official CLI, run as is.

**No Vertex leg, deliberately (the operator, 2026-07-27, task #197).** There was one:
Ollama → Vertex → AI Studio → OpenRouter, with a self-enforced spend ledger
capped at $300. It was built on the belief that Vertex usage bills against the
$300 GCP free-trial credit. It does not: Google bills AI Studio and Cloud/Vertex
against **separate credit pools**, and the trial credit is a *Cloud* credit that
does not cover AI. So that cap was never an allowance — it was a real-money
exposure ceiling on a provider we had no reason to pay for, armed on this machine
by ADC plus a `UA_VERTEX_PROJECT` in the sleep-pass unit. The leg is gone rather
than capped at zero: a dead branch that bills money when one env var flips is
worse than no branch. Do not re-add Gemini-on-GCP without a new billing fact —
`gemini_ask` already reaches the same models through the AI Studio pool that
the operator actually funded.
"""

import contextlib
import contextvars
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import config, usage_ledger
from .tools import gemini as gemini_tool

OLLAMA_BASE_URL_ENV = "UA_OLLAMA_BASE_URL"
OLLAMA_MODEL_ENV = "UA_OLLAMA_MODEL"
OLLAMA_TIMEOUT_ENV = "UA_OLLAMA_TIMEOUT_S"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_MODEL_ENV = "UA_OPENROUTER_MODEL"
SUBSCRIPTION_POOL_ENABLED_ENV = "UA_SUBSCRIPTION_POOL_ENABLED"
SUBSCRIPTION_POOL_URL_ENV = "UA_SUBSCRIPTION_POOL_URL"
SUBSCRIPTION_POOL_MODEL_ENV = "UA_SUBSCRIPTION_POOL_MODEL"
SUBSCRIPTION_POOL_API_KEY_ENV = "UA_SUBSCRIPTION_POOL_API_KEY"
MEMORY_PROVIDER_ENV = "UA_MEMORY_PROVIDER"
MEMORY_MODEL_ENV = "UA_MEMORY_MODEL"
POWER_SUPPLY_PATH_ENV = "UA_POWER_SUPPLY_PATH"
CLAUDE_CLI_BIN_ENV = "UA_CLAUDE_CLI_BIN"
CLAUDE_CLI_MODEL_ENV = "UA_CLAUDE_CLI_MODEL"
CLAUDE_CLI_TIMEOUT_ENV = "UA_CLAUDE_CLI_TIMEOUT_S"

# Every env var that `leg_status()` reads to decide a leg's configured/
# explicit/reachable state — i.e. the full set a caller must clear to get a
# byte-identical "nothing configured" read. Doctor's llm_provider_chain
# check (tools/doctor.py::_llm_provider_chain_check) goes through
# `leg_status()` rather than these names directly, and the cold-start
# hermeticity fixture (tests/conftest.py::isolated_env, task #1294) clears
# exactly this tuple instead of a hand-copied list, so a new leg's env var
# can't silently leave a gap in the test isolation.
PROVIDER_CHAIN_ENV_VARS: tuple[str, ...] = (
    CLAUDE_CLI_BIN_ENV,
    CLAUDE_CLI_MODEL_ENV,
    CLAUDE_CLI_TIMEOUT_ENV,
    MEMORY_PROVIDER_ENV,
    MEMORY_MODEL_ENV,
    OPENROUTER_MODEL_ENV,
    OLLAMA_BASE_URL_ENV,
    OLLAMA_MODEL_ENV,
    OPENROUTER_KEY_ENV,
    SUBSCRIPTION_POOL_ENABLED_ENV,
    SUBSCRIPTION_POOL_URL_ENV,
    SUBSCRIPTION_POOL_MODEL_ENV,
    SUBSCRIPTION_POOL_API_KEY_ENV,
)

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"
DEFAULT_POWER_SUPPLY_PATH = "/sys/class/power_supply"
DEFAULT_OLLAMA_MODEL = "llama3:8b"
# An exact, pinned model id (never a moving alias such as ~latest): the free
# Llama 3 8B route this replaced was withdrawn. Present in OpenRouter's public
# /api/v1/models with structured_outputs (checked 2026-10-13); doctor checks
# that the configured id is still listed.
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_SUBSCRIPTION_POOL_MODEL = "subscription-pool"
DEFAULT_CLAUDE_CLI_MODEL = "claude-haiku-5-5"
REQUEST_TIMEOUT_S = 30.0
# A headless CLI session pays process start-up and login refresh on top of
# generation, so its default bound is wider than an HTTP leg's. An override is
# accepted only when finite and positive, and is clamped to the maximum.
CLAUDE_CLI_TIMEOUT_S = 120.0
CLAUDE_CLI_MAX_TIMEOUT_S = 600.0
# Output caps, enforced while the CLI runs: past either one the process
# group is killed and the leg fails, so a runaway child cannot fill memory.
CLAUDE_CLI_MAX_OUTPUT_CHARS = 1024 * 1024
CLAUDE_CLI_MAX_STDERR_BYTES = 64 * 1024
# The subscription preflight (`claude auth status --json`): its own bound, and
# how long an auth verdict is reused. The short success TTL and credential
# file metadata in the key bound stale authorization after an in-place login
# change; HOME and CLAUDE_CONFIG_DIR keep separate profiles from sharing it.
CLAUDE_CLI_AUTH_TIMEOUT_S = 15.0
CLAUDE_CLI_AUTH_TTL_S = 5.0
CLAUDE_CLI_AUTH_FAIL_TTL_S = 2.0
# Flags claude_cli_argv relies on for isolation that an older CLI may not
# have. `claude --help` must list each one, else the leg fails closed rather
# than send completions that would all fail on an unknown option.
CLAUDE_CLI_REQUIRED_FLAGS: tuple[str, ...] = ("--safe-mode", "--permission-prompts")
CLAUDE_CLI_HELP_TIMEOUT_S = 15.0
# Where a scheduled job finds the CLI when its PATH is minimal (a LaunchAgent
# gets /usr/bin:/bin:/usr/sbin:/sbin): the native installer's target, then
# Homebrew on Apple silicon and Intel. `~` is the run's HOME.
CLAUDE_CLI_FALLBACK_PATHS: tuple[str, ...] = (
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)
# Used when a caller passes no system prompt: without one, the CLI would load
# its own coding-agent prompt, which is the wrong persona for a completion.
DEFAULT_CLAUDE_CLI_SYSTEM = (
    "You are a concise assistant answering one request. Reply with the answer only."
)

# Shared schema for memory-cluster adjudication (consolidate.py, sleep_pass.py).
# Passed to gemini_ask on the AI Studio leg; Ollama/OpenRouter legs attempt
# OpenAI-shaped JSON mode and fall through when the reply is not valid JSON.
ADJUDICATION_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["DUPLICATE", "CONTRADICTION", "UNRELATED"],
        },
        "reason": {"type": "string"},
        # Task #2022: the structured self-consistency verdict. Optional, so a
        # model that omits them still satisfies the schema and gets the
        # annotate-only behaviour of task #1778. consolidate reads them strictly
        # (`structured_self_consistency`); only that reader can downgrade.
        "self_consistent": {"type": "boolean"},
        "conflicting_attributes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "reason"],
}

# The legacy plain-text verdict. It must open a line (after optional
# indentation): a VERDICT: phrase quoted inside other text is not one (#2089).
_VERDICT_RX = re.compile(
    r"^[ \t]*VERDICT:\s*(DUPLICATE|CONTRADICTION|UNRELATED)", re.IGNORECASE | re.MULTILINE
)
_REASON_RX = re.compile(r"REASON:\s*(.+)")
_ADJUDICATION_VERDICTS = ("DUPLICATE", "CONTRADICTION", "UNRELATED")


class DuplicateKeyError(ValueError):
    """A JSON object in a model reply repeats a key."""


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    """``object_pairs_hook`` that refuses a repeated key at any depth.

    Plain json.loads keeps the last of duplicate keys, so a reply such as
    ``{"verdict": "CONTRADICTION", "verdict": "DUPLICATE"}`` reads as whichever
    came last. A model verdict that says two things is not a verdict.
    """
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise DuplicateKeyError("duplicate key")
    return dict(pairs)


def _parse_json_verdict(text: str) -> tuple[str, str] | None:
    """(verdict, reason) from one strict JSON object, or None. Never raises."""
    try:
        # A duplicate key at any depth is a ValueError (DuplicateKeyError).
        data = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict", "")).upper()
    reason = str(data.get("reason", "")).strip()
    if verdict in _ADJUDICATION_VERDICTS and reason:
        return verdict, reason
    return None


def parse_adjudication_response(text: str) -> tuple[str, str] | None:
    """Parse structured JSON or legacy VERDICT:/REASON: text.

    Returns (verdict, reason) on success, or None when neither form is usable.

    Fail closed on JSON (task #2089). A reply whose stripped text starts with
    ``{``, ``[`` or a code fence is JSON-shaped: it is read only as one strict
    JSON object (the whole reply, or the body of one ```json or bare ``` fence
    that is the whole reply), with no duplicate key at any depth, a known
    verdict and a non-empty reason. Anything else about it is None, never
    re-read by the legacy pattern: a malformed object whose text holds
    ``VERDICT: DUPLICATE`` would otherwise authorize a merge proposal, and
    last-key-wins could turn CONTRADICTION into DUPLICATE (PR #1213 review).
    The legacy pattern reads only genuinely plain text: no ``{`` and no code
    fence anywhere, and the VERDICT: line opening a line.
    """
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        return _parse_json_verdict(stripped)
    if stripped.startswith("```"):
        body = _strip_json_fence(stripped)
        if body == stripped:  # not exactly one closed ```json or bare ``` fence
            return None
        return _parse_json_verdict(body)
    if "{" in text or "```" in text:
        return None
    verdict_match = _VERDICT_RX.search(text)
    if not verdict_match:
        return None
    reason_match = _REASON_RX.search(text)
    reason = reason_match.group(1).strip() if reason_match else text.strip()[:200]
    return verdict_match.group(1).upper(), reason


def _validate_json_property(value: Any, spec: dict[str, Any]) -> bool:
    """Validate one JSON value against a small JSON-Schema property fragment."""
    expected_type = spec.get("type")
    if expected_type == "string":
        if not isinstance(value, str) or not value.strip():
            return False
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            return False
        return True
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "array":
        if not isinstance(value, list):
            return False
        items = spec.get("items")
        if not isinstance(items, dict):
            return True
        return all(_validate_json_item(item, items) for item in value)
    return True


def _validate_json_item(value: Any, spec: dict[str, Any]) -> bool:
    """Validate one array item. A string item may be empty; otherwise as a property."""
    if spec.get("type") == "string":
        enum = spec.get("enum")
        return isinstance(value, str) and (enum is None or value in enum)
    return _validate_json_property(value, spec)


def _response_matches_schema(text: Any, response_schema: dict[str, Any] | None) -> bool:
    """Best-effort check that a provider reply satisfies a JSON schema request."""
    if not isinstance(text, str):
        return False
    if response_schema is None:
        return True
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        # A duplicate key fails the schema: last-key-wins would let the
        # consumer read a different value than the one validated here.
        data = json.loads(stripped, object_pairs_hook=reject_duplicate_keys)
    except (ValueError, RecursionError):
        # RecursionError: a deeply nested reply must fail the schema, not
        # escape the leg (task #2025 review). ValueError covers DuplicateKeyError.
        return False
    if not isinstance(data, dict):
        return False
    if response_schema.get("type") == "object" and not isinstance(data, dict):
        return False
    required = response_schema.get("required") or []
    if not all(field in data and data[field] not in (None, "") for field in required):
        return False
    properties = response_schema.get("properties") or {}
    for field, spec in properties.items():
        if field not in data:
            continue
        if not isinstance(spec, dict) or not _validate_json_property(data[field], spec):
            return False
    return True


# Per-leg failure record (task #1639). Every leg degrades to None, never
# raises, so `complete()` could only ever say *that* a leg failed — never why.
# A nightly sleep pass then failed for days with four `ok: false` legs and no
# cause on record (a missing Ollama model, depleted AI Studio credit, a proxy
# that was not running). Legs now note a failure here before returning None;
# `complete()` resets it per leg and copies it into the route.
#
# What is recorded is deliberately NOT provider text. A provider error message,
# a gemini_ask ToolError, or an httpx error string can echo request content —
# private memory notes in the sleep pass's case — or request URLs, and the
# route lands in scheduled telemetry and logs. So a failure is three fixed
# facts: `error` (an exception class name or a category below), `status` (the
# HTTP status, when there is one) and `reason` (one entry of LEG_REASONS,
# chosen by mapping the status or exception type). Provider text may be
# *inspected* to pick a category (e.g. a 404 that names a model); it is never
# stored.
#
# A ContextVar rather than a module global so concurrent calls on different
# threads or tasks cannot read each other's failures.
_LEG_FAILURE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "llm_client_leg_failure", default=None
)

UNCONFIGURED_REASONS: dict[str, str] = {
    name: f"{name} unset"
    for name in (
        gemini_tool.API_KEY_ENV,
        OPENROUTER_KEY_ENV,
        SUBSCRIPTION_POOL_ENABLED_ENV,
        SUBSCRIPTION_POOL_URL_ENV,
    )
}

CLAUDE_CLI_MISSING_REASON = "claude binary missing"
CLAUDE_CLI_NOT_SUBSCRIPTION_REASON = "claude_cli_not_subscription"
CLAUDE_CLI_MODEL_MISMATCH_REASON = "model mismatch"
CLAUDE_CLI_UNSUPPORTED_REASON = "unsupported CLI version"

# The only strings a failed leg's `reason` can ever hold.
LEG_REASONS: frozenset[str] = frozenset(
    {
        CLAUDE_CLI_MISSING_REASON,
        CLAUDE_CLI_NOT_SUBSCRIPTION_REASON,
        CLAUDE_CLI_MODEL_MISMATCH_REASON,
        CLAUDE_CLI_UNSUPPORTED_REASON,
        "bad request",
        "auth rejected",
        "insufficient credits",
        "model not found",
        "not found",
        "rate limited",
        "timeout",
        "provider error",
        "http error",
        "connection failed",
        "transport error",
        "malformed reply",
        "empty reply",
        "schema mismatch",
        "request failed",
        "no result",
        "use a node worker",
        *UNCONFIGURED_REASONS.values(),
    }
)

_STATUS_REASONS: dict[int, str] = {
    400: "bad request",
    401: "auth rejected",
    402: "insufficient credits",
    403: "auth rejected",
    404: "not found",
    408: "timeout",
    429: "rate limited",
    504: "timeout",
}

_MODEL_NOT_FOUND_RX = re.compile(r"\bmodel\b.*\bnot (?:found|exist)", re.IGNORECASE)


def _status_reason(status: int, provider_text: str = "") -> str:
    """Category for an HTTP status. `provider_text` only refines the category."""
    if status == 404 and _MODEL_NOT_FOUND_RX.search(provider_text):
        return "model not found"
    if status in _STATUS_REASONS:
        return _STATUS_REASONS[status]
    if 500 <= status < 600:
        return "provider error"
    return "http error"


def _note_failure(error: str, reason: str, status: int | None = None) -> None:
    """Record why the current leg is about to return None.

    `reason` outside LEG_REASONS is replaced, never stored, so a future caller
    cannot route free text into the record by mistake.
    """
    record: dict[str, Any] = {
        "error": error,
        "reason": reason if reason in LEG_REASONS else "request failed",
    }
    if status is not None:
        record["status"] = status
    _LEG_FAILURE.set(record)


def _provider_error_message(resp: httpx.Response) -> str:
    """The provider's own `error.message` (OpenAI-compatible shape), else "".

    Used only to classify a status into a category; never recorded.
    """
    try:
        data = resp.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        return msg if isinstance(msg, str) else ""
    return err if isinstance(err, str) else ""


def _note_request_failure(exc: Exception) -> None:
    """Classify an exception from an OpenAI-shaped `_chat` call: class + status only."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        reason = _status_reason(status, _provider_error_message(exc.response))
        _note_failure(type(exc).__name__, reason, status)
    elif isinstance(exc, httpx.TimeoutException):
        _note_failure(type(exc).__name__, "timeout")
    elif isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        _note_failure(type(exc).__name__, "connection failed")
    elif isinstance(exc, (httpx.HTTPError, httpx.InvalidURL)):
        _note_failure(type(exc).__name__, "transport error")
    else:
        # KeyError/IndexError/ValueError: the reply was not the expected shape.
        _note_failure("MalformedResponse", "malformed reply")


def _note_ai_studio_failure(exc: ToolError) -> None:
    """Classify a gemini_ask ToolError without recording its message.

    gemini_ask raises `from` the SDK's APIError, whose integer `code` is the
    HTTP status; that is all that is kept. The message text is consulted only
    to recognise the unset-key and empty-reply cases gemini_ask raises itself.
    """
    cause = exc.__cause__
    code = getattr(cause, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        _note_failure(type(exc).__name__, _status_reason(code), code)
        return
    text = str(exc)
    if gemini_tool.API_KEY_ENV in text and "needs" in text:
        _note_failure("Unconfigured", UNCONFIGURED_REASONS[gemini_tool.API_KEY_ENV])
    elif text.startswith("Gemini returned"):
        _note_failure(type(exc).__name__, "empty reply")
    elif cause is not None and isinstance(cause, httpx.TimeoutException):
        _note_failure(type(exc).__name__, "timeout")
    else:
        _note_failure(type(exc).__name__, "request failed")


def _note_schema_mismatch() -> None:
    _note_failure("SchemaMismatch", "schema mismatch")


class LLMConnectionError(RuntimeError):
    """Every provider in the fallback chain failed or was unavailable.

    Sleep-pass callers let this propagate to a non-zero exit (design doc D3:
    hard-fail on LLM connection failure, so the systemd timer's failure state
    actually pages instead of silently producing an empty proposal).

    Carries `route` — the same ordered {target, ok, elapsed_s} records
    `complete()` returns on success — so the all-providers-unavailable case, the
    one where a caller or log line most needs to know what was tried and how
    long each leg took, isn't the one case where that diagnostic is thrown away
    (task #445 / PR #192 review).
    """

    def __init__(self, message: str, route: list[dict] | None = None) -> None:
        super().__init__(message)
        self.route = route if route is not None else []


def is_on_battery() -> bool:
    """Best-effort Linux battery check (design doc D8). False = mains/undetectable.

    Only ever used to *skip* LLM work, never to fail the pass — an undetectable
    power state (desktop, container, non-Linux) must not block a legitimate run.
    Path overridable via UA_POWER_SUPPLY_PATH (same hermeticity pattern as the
    anomaly rules' UA_ANOMALY_MEMINFO_PATH) — an empty override reads as "no
    power supply info available" rather than probing the real host.
    """
    raw = os.environ.get(POWER_SUPPLY_PATH_ENV, DEFAULT_POWER_SUPPLY_PATH)
    if not raw:
        return False
    base = Path(raw)
    try:
        if not base.is_dir():
            return False
        for entry in base.iterdir():
            type_file = entry / "type"
            if not type_file.is_file() or type_file.read_text().strip() != "Battery":
                continue
            status_file = entry / "status"
            status = status_file.read_text().strip() if status_file.is_file() else ""
            if status.lower() == "discharging":
                return True
        return False
    except OSError:
        return False


def _ollama_timeout_s() -> float:
    """Ollama leg HTTP timeout (default 30s).

    Override with UA_OLLAMA_TIMEOUT_S. Ollama is not in the default chain
    (task #2025); this bounds explicit `_call_ollama` callers.
    """
    raw = os.environ.get(OLLAMA_TIMEOUT_ENV, "").strip()
    try:
        val = float(raw)
        return val if val > 0 else REQUEST_TIMEOUT_S
    except ValueError:
        return REQUEST_TIMEOUT_S


def _schema_instruction(response_schema: dict[str, Any]) -> str:
    """Output-format instruction for OpenAI-shaped legs given a JSON schema."""
    return (
        "Output format: ignore any other reply-format instruction above. Reply with "
        "only a single JSON object, no prose, that matches this JSON Schema exactly "
        "(same key names and case): " + json.dumps(response_schema, sort_keys=True)
    )


def _chat(
    base_url: str,
    model: str,
    prompt: str,
    system: str | None,
    headers: dict,
    *,
    response_schema: dict[str, Any] | None = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
    constrain_schema: bool = False,
) -> tuple[Any, dict[str, Any] | None, str | None]:
    """Shared OpenAI-chat-completions-shaped POST for the HTTP legs.

    Returns ``(text, usage, reported_model)``: ``usage`` is the response's own
    ``usage`` object when present (an OpenAI-compatible endpoint may omit it),
    for the caller to record as hub-observed usage, and ``reported_model`` is
    the response's own ``model`` field (see _reported_http_model), the
    evidence of which model answered. Neither is fabricated here.

    With ``response_schema`` the model is told the schema, not just "reply in
    JSON" (task #1639): JSON mode alone let a model answer a caller's legacy
    ``VERDICT:/REASON:`` prompt as ``{"VERDICT": ..., "REASON": ...}``, which
    fails the schema's lowercase ``required`` keys on every call. The schema is
    appended to the system message for every leg, and ``constrain_schema``
    additionally sends ``response_format: json_schema`` — constrained decoding,
    which Ollama supports; other OpenAI-compatible endpoints keep the more
    widely supported ``json_object`` mode.
    """
    messages = []
    if response_schema is not None:
        system = (f"{system}\n\n" if system else "") + _schema_instruction(response_schema)
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if response_schema is not None:
        if constrain_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": response_schema},
            }
        else:
            body["response_format"] = {"type": "json_object"}
    resp = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json=body,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return None, None, None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    reported = _reported_http_model(data)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, usage, reported
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None, usage, reported
    return message.get("content"), usage, reported


def _reported_http_model(data: dict) -> str | None:
    """The `model` an OpenAI-shaped reply names, or None when it names none.

    An id outside _MODEL_ID_RX is recorded as "unrecognized", never verbatim:
    the field is provider text and lands in the usage ledger.
    """
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    model = model.strip()
    return model if _MODEL_ID_RX.fullmatch(model) else "unrecognized"


# A dated snapshot suffix, as in OpenRouter's canonical slugs
# (anthropic/claude-haiku-5.5 -> anthropic/claude-haiku-5.5-20261007).
_DATED_SNAPSHOT_RX = re.compile(r"\d{8}")


def _canonical_model_id(model: str) -> str:
    """One spelling for a Claude id: OpenRouter's `anthropic/claude-haiku-5.5`
    and the native `claude-haiku-5-5` are one model (docs/memory-agent-model.md).
    Any other id is returned unchanged."""
    if model.startswith(("anthropic/claude-", "claude-")):
        return model.removeprefix("anthropic/").replace(".", "-")
    return model


def _http_model_matches(requested: str, reported: str) -> bool:
    """Whether an HTTP reply's reported model is the requested one (task #2025).

    Exact, or equal under one of two documented mappings: the Claude
    native/OpenRouter spelling (_canonical_model_id), and an undated id
    answered by its dated snapshot (`<id>-YYYYMMDD`, the form of OpenRouter's
    canonical slugs and of Anthropic's dated model ids). Nothing else counts:
    another family, size, variant (`:free`) or suffix is a different model.
    """
    if reported == requested:
        return True
    want, got = _canonical_model_id(requested), _canonical_model_id(reported)
    if got == want:
        return True
    if not got.startswith(want + "-") or re.search(r"-\d{8}$", want):
        return False
    return _DATED_SNAPSHOT_RX.fullmatch(got[len(want) + 1 :]) is not None


def _accept_http_reply(
    provider: str,
    requested: str,
    *,
    pinned: bool,
    text: Any,
    usage: dict[str, Any] | None,
    reported: str | None,
    response_schema: dict[str, Any] | None,
    cost_usd: float | None,
    actor: str | None,
    run_id: Any,
) -> dict | None:
    """Validate one OpenAI-shaped reply and record its usage (task #2025 review).

    The model is labelled from the reply's own `model` field, as on the
    claude_cli leg (_recorded_model): a pinned request needs evidence that
    names the requested model, an unpinned one records missing evidence as
    "unknown", and a reply naming another model fails closed with reason
    "model mismatch" so the chain treats the leg as failed. Matching follows
    _http_model_matches.

    Usage is recorded whether or not the reply is accepted: a rejected reply
    was still generated (and on a metered route, billed). An accepted reply is
    recorded once under the model it reported; a rejected one is recorded
    once under its reported model ("unknown" when it named none), and only
    when it carries usage evidence, so a reply with no usage adds no row.
    """
    usage = usage or {}
    observed = {
        "input_tokens": usage_ledger.token_count(usage.get("prompt_tokens")),
        "output_tokens": usage_ledger.token_count(usage.get("completion_tokens")),
        "cost_usd": cost_usd,
    }

    def record(model: str) -> None:
        usage_ledger.record_observed(
            provider=provider, model=model, actor=actor, run_id=run_id, **observed
        )

    used_model = _recorded_model(
        requested,
        [reported] if reported else [],
        require_exact=pinned,
        same_model=_http_model_matches,
    )
    if (
        used_model is not None
        and isinstance(text, str)
        and _response_matches_schema(text, response_schema)
    ):
        record(used_model)
        return {"text": text, "provider": provider, "model": used_model}
    if any(value is not None for value in observed.values()):
        record(reported or "unknown")
    if used_model is None:
        _note_failure("ModelMismatch", CLAUDE_CLI_MODEL_MISMATCH_REASON)
    elif not isinstance(text, str):
        _note_failure("MalformedReply", "malformed reply")
    else:
        _note_schema_mismatch()
    return None


def _call_ollama(
    prompt: str,
    system: str | None,
    *,
    response_schema: dict[str, Any] | None = None,
    response_mime_type: str | None = None,
    model_override: str | None = None,
) -> dict | None:
    del response_mime_type  # OpenAI-shaped legs infer JSON mode from schema presence.
    base_url = os.environ.get(OLLAMA_BASE_URL_ENV) or DEFAULT_OLLAMA_BASE_URL
    model = model_override or os.environ.get(OLLAMA_MODEL_ENV) or DEFAULT_OLLAMA_MODEL
    # Snapshot actor/run before the call, not after (task #1526 follow-up,
    # Devin review of PR #875): a task can finish, or the agent can pick up a
    # new claim, while this request is in flight, and a post-hoc lookup would
    # attribute the call to the wrong run (or none).
    actor = config.verified_agent_identity()
    run_id_snapshot = usage_ledger.resolve_run_id(actor)
    try:
        text, usage, _reported = _chat(
            base_url,
            model,
            prompt,
            system,
            headers={},
            response_schema=response_schema,
            timeout_s=_ollama_timeout_s(),
            constrain_schema=True,
        )
    except (httpx.HTTPError, httpx.InvalidURL, KeyError, IndexError, ValueError) as exc:
        _note_request_failure(exc)
        return None
    # Record provider evidence before inspecting the reply body. A malformed
    # completion still consumed local model tokens, even though it cannot be
    # returned to the caller.
    usage_ledger.record_observed(
        provider="ollama",
        model=model,
        input_tokens=usage_ledger.token_count((usage or {}).get("prompt_tokens")),
        output_tokens=usage_ledger.token_count((usage or {}).get("completion_tokens")),
        cost_usd=None,
        actor=actor,
        run_id=run_id_snapshot,
    )
    if not isinstance(text, str):
        _note_failure("MalformedReply", "malformed reply")
        return None
    if not _response_matches_schema(text, response_schema):
        _note_schema_mismatch()
        return None
    return {"text": text, "provider": "ollama", "model": model}


def _call_ai_studio(
    prompt: str,
    system: str | None,
    *,
    response_schema: dict[str, Any] | None = None,
    response_mime_type: str | None = None,
    model_override: str | None = None,
) -> dict | None:
    """Google AI Studio (Gemini API key) leg — delegates to tools.gemini.gemini_ask
    so auth (header, never a URL param) and error handling live in one place.
    Returns None (never raises) on any failure: unset key, request error, or a
    blocked/empty response, matching this chain's degrade-don't-raise contract."""
    try:
        result = gemini_tool.gemini_ask(
            prompt,
            system=system,
            **({"model": model_override} if model_override else {}),
            response_schema=response_schema,
            response_mime_type=response_mime_type,
        )
    except ToolError as exc:
        _note_ai_studio_failure(exc)
        return None
    text = result["text"]
    if response_schema is not None and not _response_matches_schema(text, response_schema):
        _note_schema_mismatch()
        return None
    return {"text": text, "provider": "ai_studio", "model": result["model"]}


def _call_openrouter(
    prompt: str,
    system: str | None,
    *,
    response_schema: dict[str, Any] | None = None,
    response_mime_type: str | None = None,
    model_override: str | None = None,
) -> dict | None:
    del response_mime_type
    api_key = os.environ.get(OPENROUTER_KEY_ENV, "")
    if not api_key:
        _note_failure("Unconfigured", UNCONFIGURED_REASONS[OPENROUTER_KEY_ENV])
        return None
    # Blank reads as unset, as in leg_status: never an empty model id.
    configured = (os.environ.get(OPENROUTER_MODEL_ENV) or "").strip()
    model = model_override or configured or DEFAULT_OPENROUTER_MODEL
    # Snapshot actor/run before the call — see _call_ollama for why.
    actor = config.verified_agent_identity()
    run_id_snapshot = usage_ledger.resolve_run_id(actor)
    try:
        text, usage, reported = _chat(
            "https://openrouter.ai/api/v1",
            model,
            prompt,
            system,
            headers={"Authorization": f"Bearer {api_key}"},
            response_schema=response_schema,
        )
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        _note_request_failure(exc)
        return None
    # Hub-observed usage (task #1526): OpenRouter's usage object may carry a
    # `cost` field (same shape the broker's _record_result reads); absent it,
    # cost stays None rather than being priced at 0. A memory pin or
    # UA_OPENROUTER_MODEL pins the model, as UA_CLAUDE_CLI_MODEL does.
    return _accept_http_reply(
        "openrouter",
        model,
        pinned=model_override is not None or bool(configured),
        text=text,
        usage=usage,
        reported=reported,
        response_schema=response_schema,
        cost_usd=usage_ledger.money((usage or {}).get("cost")),
        actor=actor,
        run_id=run_id_snapshot,
    )


# The claude_cli child's environment is an allowlist (task #2025), not a copy:
# the hub's environment holds agent tokens, chat bot tokens and provider keys,
# none of which the CLI needs. Kept: what a process needs to find its home,
# binaries and locale (LC_* by prefix) and the CLI's own config dir. TMPDIR is
# set to the run's private dir. Every ANTHROPIC_* variable (API key, auth
# token, base URL) and every CLAUDE_CODE_* switch (Bedrock/Vertex/Foundry
# routing, CLAUDE_CODE_SIMPLE) is thereby gone. So is CLAUDE_CODE_OAUTH_TOKEN
# (`claude setup-token`): the CLI reports it as authMethod oauth_token, which
# the subscription preflight refuses, so forwarding it could only fail.
_CLAUDE_CLI_ENV_ALLOW = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "TERM",
        "SHELL",
        "CLAUDE_CONFIG_DIR",
    }
)

# Categories for a CLI result that reports an error. The message is inspected
# to pick one and never stored: it can echo the prompt.
_CLAUDE_CLI_AUTH_RX = re.compile(
    r"/login|not logged in|log ?in again|invalid api key|oauth|authenticat|unauthori[sz]ed",
    re.IGNORECASE,
)
_CLAUDE_CLI_LIMIT_RX = re.compile(r"usage limit|rate limit|limit reached|too many requests", re.I)
# What `claude auth status --json` must say before a completion is sent. Only
# these fields and subscriptionType are ever read from it: the output also
# carries the account email and organization, which are never kept.
_CLAUDE_SUBSCRIPTION_AUTH = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
}
_AUTH_FIELD_RX = re.compile(r"[A-Za-z0-9_.-]{1,40}")
_CLAUDE_AUTH_CACHE_MAX = 16
_claude_auth_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_claude_auth_lock = threading.Lock()


def _claude_cli_timeout_s(env: Mapping[str, str] | None = None) -> float:
    """claude_cli leg timeout: UA_CLAUDE_CLI_TIMEOUT_S if finite and positive
    (clamped to CLAUDE_CLI_MAX_TIMEOUT_S), else the 120s default. An unbounded
    value such as `inf` once reached communicate() and raised OverflowError."""
    source = os.environ if env is None else env
    raw = source.get(CLAUDE_CLI_TIMEOUT_ENV, "").strip()
    try:
        val = float(raw)
    except ValueError:
        return CLAUDE_CLI_TIMEOUT_S
    if not math.isfinite(val) or val <= 0:
        return CLAUDE_CLI_TIMEOUT_S
    return min(val, CLAUDE_CLI_MAX_TIMEOUT_S)


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _search_claude_cli(env: Mapping[str, str]) -> str | None:
    """Find `claude` on PATH, then in the usual install locations.

    `env` supplies PATH and HOME when it has them, else this process's. The
    fallback list exists for scheduled jobs: a macOS LaunchAgent starts with
    /usr/bin:/bin:/usr/sbin:/sbin, which holds neither the native installer's
    ~/.local/bin nor Homebrew's bin.

    Only absolute PATH entries are searched. An empty entry or "." means the
    current directory and any other relative entry is relative to it, so
    either would run whatever `claude` sits where the hub was started.
    """
    raw_path = env.get("PATH") or os.environ.get("PATH") or ""
    search = os.pathsep.join(d for d in raw_path.split(os.pathsep) if os.path.isabs(d))
    found = shutil.which("claude", path=search) if search else None
    if found:
        return found
    home = env.get("HOME") or os.environ.get("HOME") or str(Path.home())
    for candidate in CLAUDE_CLI_FALLBACK_PATHS:
        path = Path(home + candidate[1:]) if candidate.startswith("~/") else Path(candidate)
        if _is_executable_file(path):
            return str(path)
    return None


def resolve_claude_cli(env: Mapping[str, str] | None = None) -> str | None:
    """The absolute path of the `claude` the claude_cli leg will run, or None.

    UA_CLAUDE_CLI_BIN, when set, is the only candidate: a wrong explicit path
    reads as missing rather than silently running some other install. A
    relative value is made absolute against the current directory now, before
    the run moves to its private working dir. Doctor uses this same resolver.
    """
    env = os.environ if env is None else env
    from . import execution_locality

    # Task #2109: on a devices-only hub the leg does not exist, so completions
    # fall through to the HTTP legs and doctor reads it as unconfigured.
    if execution_locality.devices_only(env):
        return None
    explicit = (env.get(CLAUDE_CLI_BIN_ENV) or "").strip()
    if explicit:
        # Absolute before the run changes its working dir (Devin, PR #1157).
        path = Path(os.path.abspath(Path(explicit).expanduser()))
        return str(path) if _is_executable_file(path) else None
    return _search_claude_cli(env)


def claude_cli_argv(binary: str, model: str, system_prompt_file: str) -> list[str]:
    """The exact headless invocation. Neither prompt is in it: the user prompt
    goes on stdin and the system prompt in a 0600 file, so neither shows in `ps`.

    - ``-p --output-format json``: one non-interactive turn, one JSON result.
    - ``--tools ""``: no built-in tools, so this is a completion, not an agent.
    - ``--strict-mcp-config`` with no ``--mcp-config``: no MCP servers at all.
    - ``--setting-sources local``: user and project settings (their hooks,
      plugins, permissions, MCP servers) are not loaded; the only source left
      is ``.claude/settings.local.json`` in the empty temp cwd, which does not
      exist.
    - ``--no-session-persistence``: no transcript written to disk.
    - ``--safe-mode``: hooks, CLAUDE.md, skills, plugins, MCP servers and
      custom agents are all off, whatever a settings file says — a second
      wall behind ``--setting-sources``. Auth and model selection still work.
    - ``--disable-slash-commands``: no skills.
    - ``--permission-prompts none``: anything that would prompt is denied
      rather than waiting for an answer nobody will give.
    - ``--system-prompt-file``: replaces the CLI's coding-agent prompt.
    Never ``--bare``: it authenticates only with ANTHROPIC_API_KEY.
    """
    return [
        binary,
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--setting-sources",
        "local",
        "--safe-mode",
        "--disable-slash-commands",
        "--permission-prompts",
        "none",
        "--system-prompt-file",
        system_prompt_file,
    ]


def claude_cli_child_env(
    base: Mapping[str, str] | None = None, *, tmpdir: Path | None = None
) -> dict[str, str]:
    """The allowlisted subset of `base` (default os.environ); see _CLAUDE_CLI_ENV_ALLOW.

    With `tmpdir`, TMPDIR points at it instead of the shared temp dir.
    """
    base = os.environ if base is None else base
    env = {
        key: value
        for key, value in base.items()
        if key in _CLAUDE_CLI_ENV_ALLOW or key.startswith("LC_")
    }
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return env


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the CLI and anything it spawned, then reap it. Never raises.

    The group is signalled even when the CLI itself has already exited, as
    it has after every successful run: a descendant can outlive it, holding
    the output pipes or not, and the group id stays valid (and cannot be
    reused) while any member lives. With no member left the signal fails
    harmlessly (ProcessLookupError; PermissionError is ignored the same way).
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:  # ProcessLookupError, PermissionError
        pass
    if proc.returncode is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except BaseException:  # noqa: BLE001 - cleanup must not mask the original error
        pass


def _inside_git_checkout(path: Path) -> bool:
    for directory in (path, *path.parents):
        try:
            if (directory / ".git").exists():
                return True
        except OSError:
            return True  # cannot tell: treat as unsafe
    return False


def _claude_cli_run_base(env: Mapping[str, str] | None = None) -> Path | None:
    """`<UA_HOME>/tmp/claude-cli`, private (0700), or None if it is unusable.

    Runs live under the hub's own data home, not TMPDIR, so no other user's
    files and no git checkout surround them: a checkout above the run dir
    would let the CLI find that project's CLAUDE.md and settings. A base
    inside a git checkout is refused rather than used.
    """
    source = os.environ if env is None else env
    data_home = (
        Path(source["UA_HOME"]).expanduser() if source.get("UA_HOME") else config.data_home()
    )
    base = data_home / "tmp" / "claude-cli"
    try:
        base.mkdir(parents=True, exist_ok=True)
        base.chmod(0o700)
        base = base.resolve()
    except OSError:
        return None
    return None if _inside_git_checkout(base) else base


@contextlib.contextmanager
def _private_run_dir(base: Path):
    """A fresh 0700 run dir under `base`; yields (run_dir, cwd, tmpdir).

    `cwd/` is the CLI's empty working dir and `tmp/` its TMPDIR; files the run
    writes for the CLI (the system prompt) sit in run_dir beside them. The
    whole tree is removed afterwards.
    """
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    try:
        cwd = run_dir / "cwd"
        cwd.mkdir(mode=0o700)
        tmpdir = run_dir / "tmp"
        tmpdir.mkdir(mode=0o700)
        yield run_dir, cwd, tmpdir
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _write_private_file(path: Path, text: str) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class ClaudeCLIOutputTooLarge(Exception):
    """The CLI wrote more than its stdout or stderr cap."""


class _CappedPipeReader(threading.Thread):
    """Drains one pipe, keeping at most `cap` bytes; flags an overflow."""

    def __init__(self, pipe, cap: int) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._cap = cap
        self._chunks: list[bytes] = []
        self._size = 0
        self.overflow = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._pipe.read1(65536)
                if not chunk:
                    return
                if self._size + len(chunk) > self._cap:
                    self.overflow = True
                    return
                self._chunks.append(chunk)
                self._size += len(chunk)
        except (OSError, ValueError):
            return

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _feed_stdin(pipe, data: bytes) -> None:
    try:
        pipe.write(data)
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


def _run_cli(
    argv: list[str],
    stdin_text: str | None,
    timeout_s: float,
    cwd: Path,
    tmpdir: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    """Run the CLI in its own process group with the allowlisted environment.

    stdout and stderr are read with caps (CLAUDE_CLI_MAX_OUTPUT_CHARS /
    CLAUDE_CLI_MAX_STDERR_BYTES) while it runs; past either the run stops
    with ClaudeCLIOutputTooLarge. Every exit, normal or not (a timeout, an
    overflow, KeyboardInterrupt, cancellation, any other exception), kills and
    reaps the whole process group in `finally`, so no descendant outlives the
    call: a child the CLI backgrounded with its stdio detached would otherwise
    survive a successful run.
    """
    from .hosted_execution import refuse_hosted_execution

    refuse_hosted_execution("claude -p")
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=claude_cli_child_env(env, tmpdir=tmpdir),
        start_new_session=True,
    )
    readers: list[_CappedPipeReader] = []
    try:
        out = _CappedPipeReader(proc.stdout, CLAUDE_CLI_MAX_OUTPUT_CHARS)
        err = _CappedPipeReader(proc.stderr, CLAUDE_CLI_MAX_STDERR_BYTES)
        readers = [out, err]
        out.start()
        err.start()
        if stdin_text is not None:
            data = stdin_text.encode("utf-8", errors="replace")
            threading.Thread(target=_feed_stdin, args=(proc.stdin, data), daemon=True).start()
        # One deadline covers the CLI's exit *and* end-of-file on both pipes:
        # a descendant that keeps an inherited pipe open after the CLI exits
        # must not stretch the call past its bound.
        deadline = time.monotonic() + timeout_s
        while True:
            if out.overflow or err.overflow:
                raise ClaudeCLIOutputTooLarge()
            if proc.poll() is not None and not out.is_alive() and not err.is_alive():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("claude", timeout_s)
            if proc.returncode is None:
                try:
                    proc.wait(timeout=min(remaining, 0.1))
                except subprocess.TimeoutExpired:
                    pass
            else:
                (out if out.is_alive() else err).join(min(remaining, 0.1))
        if out.overflow or err.overflow:
            raise ClaudeCLIOutputTooLarge()
        return proc.returncode, out.text()
    finally:
        _kill_process_group(proc)
        for reader in readers:
            reader.join(2)
        # A pipe is closed only once its reader is done: closing a buffered
        # pipe another thread is still reading can block on its lock. stdin is
        # closed by its feeder thread.
        for pipe, reader in zip((proc.stdout, proc.stderr), readers or (None, None), strict=False):
            if pipe is not None and (reader is None or not reader.is_alive()):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass


def _parse_cli_json(stdout: str) -> tuple[dict | None, str]:
    """(data, "") or (None, error class). Size-capped; never raises."""
    if len(stdout) > CLAUDE_CLI_MAX_OUTPUT_CHARS:
        return None, "OversizedResponse"
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return None, "MalformedResponse"
    except Exception:  # noqa: BLE001 - any parser failure is a malformed reply
        return None, "MalformedResponse"
    return (data, "") if isinstance(data, dict) else (None, "MalformedResponse")


def _auth_field(value: object) -> str:
    if value is None:
        return "none"
    text = str(value)
    return text if _AUTH_FIELD_RX.fullmatch(text) else "unknown"


def _claude_cli_auth_uncached(
    binary: str, base: Path, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Run `claude auth status --json` once and keep only the auth facts."""
    try:
        with _private_run_dir(base) as (_run_dir, cwd, tmpdir):
            returncode, stdout = _run_cli(
                [binary, "auth", "status", "--json"],
                None,
                CLAUDE_CLI_AUTH_TIMEOUT_S,
                cwd,
                tmpdir,
                env=env,
            )
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": "auth status timed out"}
    except Exception as exc:  # noqa: BLE001 - the leg must fail closed, not raise
        return {"ok": False, "why": f"auth status failed: {type(exc).__name__}"}
    data, _error = _parse_cli_json(stdout)
    if returncode != 0 or data is None:
        return {"ok": False, "why": "auth status unreadable"}
    facts: dict[str, Any] = {
        "auth_method": _auth_field(data.get("authMethod")),
        "api_provider": _auth_field(data.get("apiProvider")),
        "subscription_type": _auth_field(data.get("subscriptionType")),
    }
    facts["ok"] = all(data.get(key) == want for key, want in _CLAUDE_SUBSCRIPTION_AUTH.items())
    if data.get("loggedIn") is not True:
        facts["why"] = "not logged in"
    elif not facts["ok"]:
        facts["why"] = f"authMethod={facts['auth_method']}, apiProvider={facts['api_provider']}"
    return facts


def _claude_cli_auth_cache_key(
    binary: str, base: Path, env: Mapping[str, str] | None = None
) -> tuple:
    """Bound auth-cache reuse to the CLI identity/config context, without reading credentials."""
    source = os.environ if env is None else env
    home = source.get("HOME") or str(Path.home())
    configured_dir = source.get("CLAUDE_CONFIG_DIR", "")
    config_dir = Path(configured_dir) if configured_dir else Path(home) / ".claude"
    if not config_dir.is_absolute():
        config_dir = Path(home) / config_dir
    try:
        stat = (config_dir / ".credentials.json").stat()
        credential_state = (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
    except OSError:
        credential_state = None
    try:
        binary_stat = os.stat(binary)
        binary_state = (
            binary_stat.st_dev,
            binary_stat.st_ino,
            binary_stat.st_size,
            binary_stat.st_mtime_ns,
        )
    except OSError:
        binary_state = None
    try:
        uid = os.getuid()
    except AttributeError:
        uid = None
    return (binary, binary_state, uid, home, str(config_dir), str(base), credential_state)


def _claude_cli_auth(
    binary: str, base: Path, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Whether `binary` would run on a claude.ai subscription login (task #2025).

    Stripping ANTHROPIC_* from the child's environment is not enough: stored
    Console credentials (an API-key login in the config dir or keychain) would
    still bill the API while this leg records no cost. So before any
    completion the CLI's own `auth status` must report loggedIn, authMethod
    claude.ai and apiProvider firstParty; anything else fails closed. The
    check spends nothing. Diagnostic probes may reuse a short-lived verdict,
    but generation uses the uncached validator because Keychain changes need
    not touch .credentials.json.
    """
    now = time.monotonic()
    key = _claude_cli_auth_cache_key(binary, base, env)
    with _claude_auth_lock:
        hit = _claude_auth_cache.get(key)
    if hit is not None:
        ttl = CLAUDE_CLI_AUTH_TTL_S if hit[1].get("ok") else CLAUDE_CLI_AUTH_FAIL_TTL_S
        if now - hit[0] < ttl:
            return hit[1]
    facts = _claude_cli_auth_uncached(binary, base, env)
    with _claude_auth_lock:
        if len(_claude_auth_cache) >= _CLAUDE_AUTH_CACHE_MAX:
            _claude_auth_cache.clear()
        _claude_auth_cache[key] = (time.monotonic(), facts)
    return facts


_claude_caps_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}


def _help_lists_flag(help_text: str, flag: str) -> bool:
    """Whether `flag` appears as a whole option (not `--safe-mode-legacy`)."""
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


def _claude_cli_capabilities_uncached(
    binary: str, base: Path, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Run `claude --help` once; ok only if it lists every required flag."""
    try:
        with _private_run_dir(base) as (_run_dir, cwd, tmpdir):
            returncode, stdout = _run_cli(
                [binary, "--help"], None, CLAUDE_CLI_HELP_TIMEOUT_S, cwd, tmpdir, env=env
            )
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": "--help timed out"}
    except Exception as exc:  # noqa: BLE001 - the leg must fail closed, not raise
        return {"ok": False, "why": f"--help failed: {type(exc).__name__}"}
    if returncode != 0:
        return {"ok": False, "why": "--help failed"}
    missing = [flag for flag in CLAUDE_CLI_REQUIRED_FLAGS if not _help_lists_flag(stdout, flag)]
    if missing:
        return {"ok": False, "why": "no " + ", ".join(missing)}
    return {"ok": True}


def _claude_cli_capabilities(
    binary: str, base: Path, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Whether `binary` supports every flag in CLAUDE_CLI_REQUIRED_FLAGS.

    Probed once per binary file: the cache key carries its mtime and size, so
    an upgrade in place is probed again. A failed probe is cached only for
    CLAUDE_CLI_AUTH_FAIL_TTL_S, so a slow start is retried. The probe runs
    with the same stripped environment and private dir as the auth preflight.
    """
    try:
        st = os.stat(binary)
        key: tuple = (binary, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (binary, None, None)
    with _claude_auth_lock:
        hit = _claude_caps_cache.get(key)
    if hit is not None and (
        hit[1].get("ok") or time.monotonic() - hit[0] < CLAUDE_CLI_AUTH_FAIL_TTL_S
    ):
        return hit[1]
    facts = _claude_cli_capabilities_uncached(binary, base, env)
    with _claude_auth_lock:
        if len(_claude_caps_cache) >= _CLAUDE_AUTH_CACHE_MAX:
            _claude_caps_cache.clear()
        _claude_caps_cache[key] = (time.monotonic(), facts)
    return facts


_MODEL_ID_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@\[\]/-]{0,79}")


def _cli_reported_models(data: dict) -> list[str]:
    """The distinct model ids the CLI says it used, sorted; [] when it says none."""
    models: set[str] = set()
    usage = data.get("modelUsage")
    if isinstance(usage, dict):
        models.update(key.strip() for key in usage if isinstance(key, str) and key.strip())
    model = data.get("model")
    if isinstance(model, str) and model.strip():
        models.add(model.strip())
    return sorted(m if _MODEL_ID_RX.fullmatch(m) else "unrecognized" for m in models)


def _recorded_model(
    requested: str,
    reported: list[str],
    *,
    require_exact: bool = False,
    same_model=None,
) -> str | None:
    """The truthful model label for a call, or None when it must fail closed.

    Only provider-reported evidence is considered, and the label is always
    that evidence, never the request. On the claude_cli leg matching is exact
    (claude-haiku-5-5 has no dated alias, so a lookalike id is a different
    model); the HTTP legs pass `same_model=_http_model_matches`. Pinned calls
    require a single reported model that matches; missing, mixed, or
    mismatched evidence fails closed. Unpinned calls preserve diagnostic
    behavior: no report is "unknown", a mixed report is the `+`-joined set,
    and a report with no matching model fails closed.
    """
    same = same_model or (lambda want, got: want == got)
    if not reported:
        return None if require_exact else "unknown"
    matched = any(same(requested, model) for model in reported)
    if require_exact:
        return reported[0] if matched and len(reported) == 1 else None
    return "+".join(reported) if matched else None


def _cli_input_tokens(usage: dict) -> int | None:
    """Input tokens including cache reads and writes; None when none is reported."""
    counts = [
        usage_ledger.token_count(usage.get(key))
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    ]
    reported = [count for count in counts if count is not None]
    return sum(reported) if reported else None


def _strip_json_fence(text: str) -> str:
    """Remove one ```json (or bare ```) fence around a reply; linear time.

    String operations only: an earlier regex went quadratic on a fence
    opener followed by many newlines.
    """
    stripped = text.strip()
    if len(stripped) < 6 or not stripped.startswith("```") or not stripped.endswith("```"):
        return text
    newline = stripped.find("\n")
    if newline == -1 or stripped[3:newline].strip() not in ("", "json"):
        return text
    return stripped[newline + 1 : -3].strip()


def _note_claude_cli_error(message: object) -> None:
    text = message if isinstance(message, str) else ""
    if _CLAUDE_CLI_AUTH_RX.search(text):
        reason = "auth rejected"
    elif _CLAUDE_CLI_LIMIT_RX.search(text):
        reason = "rate limited"
    else:
        reason = "provider error"
    _note_failure("ClaudeCLIError", reason)


def _call_claude_cli(
    prompt: str,
    system: str | None,
    *,
    response_schema: dict[str, Any] | None = None,
    response_mime_type: str | None = None,
    model_override: str | None = None,
) -> dict | None:
    """One completion from the Claude Code CLI on the logged-in subscription (task #2025).

    Returns None (never raises) when the binary is missing, the run dir would
    sit inside a git checkout, `claude --help` does not list every flag in
    CLAUDE_CLI_REQUIRED_FLAGS (reason "unsupported CLI version"),
    `claude auth status` does not show a claude.ai
    subscription login (reason claude_cli_not_subscription), the process fails
    or times out, the result reports an error (including "please log in"),
    the reply is oversized, malformed, empty or misses a requested schema, or
    the CLI reports models that do not include the requested id. Pinned
    model requests require the exact requested id as the CLI's sole
    reported model. Unpinned requests record mixed model evidence as a
    `+`-joined set and no evidence as "unknown". Any parsed result that
    reports token usage is recorded once in the usage ledger, accepted or
    not; a rejected one under the models the CLI reported.
    With `response_schema` the schema is added to the system prompt, as on
    the OpenAI-shaped legs, and the reply is validated; a ```json fence
    around it is removed.
    """
    del response_mime_type  # JSON mode is inferred from the schema, as on the other legs.
    from .hosted_execution import HostedExecutionError, is_hosted

    if is_hosted():
        # Do not probe or spawn ``claude``. The rest of the chain may still
        # answer over the network; this leg is a local process and stays off.
        _note_failure("HostedExecutionError", "use a node worker")
        return None
    env = dict(os.environ)
    binary = resolve_claude_cli(env)
    if binary is None:
        _note_failure("Unconfigured", CLAUDE_CLI_MISSING_REASON)
        return None
    base = _claude_cli_run_base(env)
    if base is None:
        _note_failure("UnsafeWorkdir", "request failed")
        return None
    if not _claude_cli_capabilities(binary, base, env).get("ok"):
        _note_failure("UnsupportedCLI", CLAUDE_CLI_UNSUPPORTED_REASON)
        return None
    # Keychain-backed profiles can change without touching .credentials.json.
    # Never use the diagnostic cache to authorize generation.
    if not _claude_cli_auth_uncached(binary, base, env).get("ok"):
        _note_failure("NotSubscription", CLAUDE_CLI_NOT_SUBSCRIPTION_REASON)
        return None
    model = model_override or env.get(CLAUDE_CLI_MODEL_ENV, "").strip() or DEFAULT_CLAUDE_CLI_MODEL
    system_prompt = system or DEFAULT_CLAUDE_CLI_SYSTEM
    if response_schema is not None:
        system_prompt = f"{system_prompt}\n\n{_schema_instruction(response_schema)}"
    # Snapshot actor/run before the call — see _call_ollama for why.
    actor = config.verified_agent_identity()
    run_id_snapshot = usage_ledger.resolve_run_id(actor)
    try:
        with _private_run_dir(base) as (run_dir, cwd, tmpdir):
            system_file = _write_private_file(run_dir / "system-prompt.txt", system_prompt)
            returncode, stdout = _run_cli(
                claude_cli_argv(binary, model, str(system_file)),
                prompt,
                _claude_cli_timeout_s(env),
                cwd,
                tmpdir,
                env=env,
            )
    except subprocess.TimeoutExpired:
        _note_failure("TimeoutExpired", "timeout")
        return None
    except HostedExecutionError:
        _note_failure("HostedExecutionError", "use a node worker")
        return None
    except ClaudeCLIOutputTooLarge:
        _note_failure("OversizedResponse", "malformed reply")
        return None
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        _note_failure(type(exc).__name__, "request failed")
        return None
    data, parse_error = _parse_cli_json(stdout)
    if data is None:
        if parse_error == "MalformedResponse" and returncode != 0:
            _note_failure("ClaudeCLIExit", "request failed")
        else:
            _note_failure(parse_error, "malformed reply")
        return None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    reported_models = _cli_reported_models(data)
    observed = {
        "input_tokens": _cli_input_tokens(usage),
        "output_tokens": usage_ledger.token_count(usage.get("output_tokens")),
    }

    # Hub-observed usage (task #1526): the preflight proved a subscription
    # login, which has no per-call price. The result's total_cost_usd is an
    # API-equivalent estimate, not a charge, so cost stays None. Input tokens
    # include cache reads and writes.
    def record(model_label: str) -> None:
        usage_ledger.record_observed(
            provider="claude_cli",
            model=model_label,
            cost_usd=None,
            actor=actor,
            run_id=run_id_snapshot,
            **observed,
        )

    def record_rejected() -> None:
        # A reply the leg rejects still used subscription capacity (task #2025
        # review): record it, under the models the CLI reported, so a later
        # paid fallback cannot hide it. No usage evidence, no row.
        if any(value is not None for value in observed.values()):
            record("+".join(reported_models) if reported_models else "unknown")

    if data.get("is_error") or returncode != 0:
        record_rejected()
        _note_claude_cli_error(data.get("result"))
        return None
    text = data.get("result")
    if not isinstance(text, str) or not text.strip():
        record_rejected()
        _note_failure("EmptyResponse", "empty reply")
        return None
    if response_schema is not None:
        text = _strip_json_fence(text)
        if not _response_matches_schema(text, response_schema):
            record_rejected()
            _note_schema_mismatch()
            return None
    # The model is labelled from the CLI's own report, never assumed from the
    # request (see _recorded_model).
    require_exact_model = model_override is not None or bool(
        env.get(CLAUDE_CLI_MODEL_ENV, "").strip()
    )
    used_model = _recorded_model(model, reported_models, require_exact=require_exact_model)
    if used_model is None:
        record_rejected()
        _note_failure("ModelMismatch", CLAUDE_CLI_MODEL_MISMATCH_REASON)
        return None
    record(used_model)
    return {"text": text, "provider": "claude_cli", "model": used_model}


def _subscription_pool_enabled() -> bool:
    """Return whether the global subscription-pool opt-in is explicitly set."""
    return os.environ.get(SUBSCRIPTION_POOL_ENABLED_ENV, "0") == "1"


def _subscription_pool_headers() -> dict[str, str]:
    """Auth header for the subscription-pool endpoint, if a dedicated
    api-keys client key is configured. CLIProxyAPI (and OpenAI-compatible
    proxies generally) expect the key in an `Authorization` header (the
    `Bearer` scheme, same as the openrouter leg). The key
    itself never appears in a log line, error, or route record — only
    whether the header was attached. (Task #1253: this is the proxy's
    client `api-keys` entry, distinct from its management key.)"""
    key = os.environ.get(SUBSCRIPTION_POOL_API_KEY_ENV, "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _call_subscription_pool(
    prompt: str,
    system: str | None,
    *,
    response_schema: dict[str, Any] | None = None,
    response_mime_type: str | None = None,
    model_override: str | None = None,
) -> dict | None:
    """Call an explicitly configured OpenAI-compatible subscription endpoint.

    task #1154: the endpoint is a self-hosted proxy (CLIProxyAPI) fronting
    subscription-backed model capacity over loopback/Tailscale only — never a
    public host. Off by default: returns None immediately unless both the
    global opt-in (UA_SUBSCRIPTION_POOL_ENABLED=1) and the per-call
    `subscription_pool` flag on `complete()` are set, so no existing caller's
    behavior changes without an explicit code change at that call site.
    """
    del response_mime_type
    if not _subscription_pool_enabled():
        _note_failure("Unconfigured", UNCONFIGURED_REASONS[SUBSCRIPTION_POOL_ENABLED_ENV])
        return None
    base_url = os.environ.get(SUBSCRIPTION_POOL_URL_ENV, "").strip()
    if not base_url:
        _note_failure("Unconfigured", UNCONFIGURED_REASONS[SUBSCRIPTION_POOL_URL_ENV])
        return None
    # Blank reads as unset, as in leg_status: never an empty model id.
    configured = (os.environ.get(SUBSCRIPTION_POOL_MODEL_ENV) or "").strip()
    model = model_override or configured or DEFAULT_SUBSCRIPTION_POOL_MODEL
    # Snapshot actor/run before the call — see _call_ollama for why.
    actor = config.verified_agent_identity()
    run_id_snapshot = usage_ledger.resolve_run_id(actor)
    try:
        text, usage, reported = _chat(
            base_url,
            model,
            prompt,
            system,
            headers=_subscription_pool_headers(),
            response_schema=response_schema,
        )
    except (httpx.HTTPError, httpx.InvalidURL, KeyError, IndexError, ValueError) as exc:
        _note_request_failure(exc)
        return None
    # Hub-observed usage (task #1526): subscription capacity has no per-call
    # dollar price to observe, so cost stays None, never a fabricated 0.
    return _accept_http_reply(
        "subscription_pool",
        model,
        pinned=model_override is not None or bool(configured),
        text=text,
        usage=usage,
        reported=reported,
        response_schema=response_schema,
        cost_usd=None,
        actor=actor,
        run_id=run_id_snapshot,
    )


# Fixed order per the operator's rules of 2026-10-13 (task #2025): the headless
# Claude subscription session first, the operator's OpenRouter key second, and
# nothing else. No local model (the local judge's false-contradiction rate) and
# no Google AI Studio API key: Gemini is reachable only through the explicit
# gemini_ask tool. See the module docstring on why the Vertex leg was removed
# rather than capped.
_PROVIDER_CHAIN = [
    ("claude_cli", _call_claude_cli),
    ("openrouter", _call_openrouter),
]

# Target registry (Switchyard-inspired routing proposal, Phase 1 —
# Wiki/LLM-Routing-Proposal.md item 1): metadata about each leg, kept separate
# from the ordered policy above so a future selector can read
# capability/cost-tier facts without touching call sites. `_PROVIDER_CHAIN`
# stays the source of truth for the default order and callables; the optional
# subscription-pool leg is inserted only by an explicitly opted-in call. The
# ollama and ai_studio entries describe retained helpers; no chain selects them.
ROUTE_TARGETS = {
    "claude_cli": {"cost_tier": "subscription", "local": False, "capability": "general"},
    "openrouter": {"cost_tier": "metered", "local": False, "capability": "general"},
    "ollama": {"cost_tier": "free", "local": True, "capability": "general"},
    "ai_studio": {"cost_tier": "metered", "local": False, "capability": "general"},
    "subscription_pool": {
        "cost_tier": "subscription",
        "local": False,
        "capability": "general",
    },
}


LEG_STATUS_TIMEOUT_S = 2.0
CLAUDE_CLI_LOGIN_CAVEAT = "subscription login checked locally; quota not probed"


def _listed_model_ids(resp: httpx.Response) -> set[str] | None:
    """Model ids from an OpenAI-compatible `GET /models` reply, or None if unreadable."""
    try:
        data = resp.json()
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        return {
            row["name"]
            for row in data["models"]
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    return {row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}


def _model_listed(model: str, ids: set[str], *, exact: bool = False) -> bool:
    """Whether `model` is in a `/models` listing.

    Ollama resolves an untagged name to `:latest`, so by default either
    spelling counts; with `exact` (OpenRouter) only the identical id does.
    """
    if model in ids:
        return True
    return not exact and ":" not in model and f"{model}:latest" in ids


# Probe results reused for config.llm_probe_cache_ttl_s() (task #1850). Keyed
# by URL, a digest of the headers (never the header values themselves) and the
# expected model; holds (monotonic stamp, ok, why, timeout_s). Failures are
# cached too, so a down endpoint is not re-probed on every doctor run.
_PROBE_CACHE_MAX = 64
_probe_cache: dict[tuple, tuple[float, bool, str, float]] = {}
_probe_cache_lock = threading.Lock()


def _probe_cache_key(
    url: str, headers: dict, expect_model: str | None, ollama_alias: bool, exact_model: bool
):
    digest = hashlib.sha256(
        json.dumps(sorted((str(k), str(v)) for k, v in headers.items())).encode()
    ).hexdigest()
    return (url, digest, expect_model, ollama_alias, exact_model)


def clear_probe_cache() -> None:
    """Forget every cached leg probe (tests; an operator who just fixed a leg)."""
    with _probe_cache_lock:
        _probe_cache.clear()
    with _claude_auth_lock:
        _claude_auth_cache.clear()
        _claude_caps_cache.clear()


def _probe_endpoint(
    url: str,
    headers: dict,
    timeout_s: float,
    *,
    expect_model: str | None = None,
    ollama_alias: bool = False,
    exact_model: bool = False,
) -> tuple[bool, str]:
    """One unbilled metadata GET for leg_status, cached briefly; returns (ok, why).

    A result younger than ``config.llm_probe_cache_ttl_s()`` is reused, success
    or failure, and its ``why`` says how old it is (task #1850): doctor once
    probed a down ollama about once a second while the Control Center polled.
    """
    return _cached_probe(
        _probe_cache_key(url, headers, expect_model, ollama_alias, exact_model),
        timeout_s,
        lambda: _probe_endpoint_uncached(
            url,
            headers,
            timeout_s,
            expect_model=expect_model,
            exact_model=exact_model,
            ollama_alias=ollama_alias,
        ),
    )


def _cached_probe(key: tuple, timeout_s: float, probe) -> tuple[bool, str]:
    """Run `probe()` -> (ok, why) through the leg-probe cache (task #1850)."""
    ttl = config.llm_probe_cache_ttl_s()
    if ttl > 0:
        with _probe_cache_lock:
            hit = _probe_cache.get(key)
        if hit is not None:
            age = time.monotonic() - hit[0]
            # A failure under a shorter timeout says nothing about a caller
            # willing to wait longer, so it is re-probed (Devin, #1016). A
            # success, or a failure at least as patient, is reused.
            if age < ttl and (hit[1] or hit[3] >= timeout_s):
                return hit[1], f"{hit[2]} (cached {age:.0f}s ago)"
    ok, why = probe()
    if ttl > 0:
        with _probe_cache_lock:
            if len(_probe_cache) >= _PROBE_CACHE_MAX:
                _probe_cache.clear()
            _probe_cache[key] = (time.monotonic(), ok, why, timeout_s)
    return ok, why


def _probe_endpoint_uncached(
    url: str,
    headers: dict,
    timeout_s: float,
    *,
    expect_model: str | None = None,
    ollama_alias: bool = False,
    exact_model: bool = False,
) -> tuple[bool, str]:
    """One unbilled metadata GET for leg_status; returns (ok, why).

    Never raises, and `why` carries only the exception type, the HTTP status
    and, with `expect_model`, whether that model is in the listing — never a
    response body, which could echo the credential back.

    `expect_model` (task #1639): a `/models` endpoint that answers 200 proves
    the server is up, not that the leg can generate. Ollama answered 200 for
    weeks while every chat call 404'd on a model that was never pulled, and
    doctor reported the leg up. With `expect_model` the leg is only up when the
    model the chain will request is actually listed.
    """
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout_s)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        # InvalidURL is not an HTTPError, and a non-ASCII key (a pasted smart
        # quote) raises UnicodeEncodeError, a ValueError, from header encoding:
        # either must mark this leg down, not abort the whole leg_status read.
        return False, type(e).__name__
    if not 200 <= resp.status_code < 300:
        return False, f"HTTP {resp.status_code}"
    if expect_model is None:
        return True, f"HTTP {resp.status_code}"
    ids = _listed_model_ids(resp)
    if ids is None:
        return False, f"HTTP {resp.status_code}, model list unreadable"
    listed = _model_listed(expect_model, ids) if ollama_alias else expect_model in ids
    if exact_model:
        listed = expect_model in ids
    if not listed:
        return False, f"model {expect_model} not {'listed' if exact_model else 'pulled'}"
    return True, f"HTTP {resp.status_code}, model {expect_model} present"


def _ollama_leg_status(env: Mapping[str, str], *, timeout_s: float) -> dict:
    """The ollama leg_status entry for `env` — up only when its model is pulled.

    An empty UA_OLLAMA_* value means the default, at run time and here alike
    (task #1639), so doctor never probes a different model than a run uses.
    """
    base_url = env.get(OLLAMA_BASE_URL_ENV) or DEFAULT_OLLAMA_BASE_URL
    model = env.get(OLLAMA_MODEL_ENV) or DEFAULT_OLLAMA_MODEL
    ok, why = _probe_endpoint(
        f"{base_url.rstrip('/')}/models", {}, timeout_s, expect_model=model, ollama_alias=True
    )
    return {
        "leg": "ollama",
        "configured": True,
        "explicit": bool(env.get(OLLAMA_BASE_URL_ENV) or env.get(OLLAMA_MODEL_ENV)),
        "reachable": ok,
        "detail": f"{base_url}: {why}",
    }


def _claude_cli_leg_status(env: Mapping[str, str], *, timeout_s: float) -> dict:
    """The claude_cli leg_status entry: up only on a claude.ai subscription login.

    Runs the same `claude --help` flag probe and `claude auth status --json`
    preflight the leg runs (both cached), never a completion, so it spends
    nothing; a CLI too old for the leg's flags reads as down, not up. Only authMethod and
    subscriptionType are reported; the email and organization in that output
    are never read out. A missing binary is unconfigured, not down: a host
    without the CLI simply has no such leg. `timeout_s` is unused: the
    preflight has its own bound. The binary is resolved from `env`; the
    preflight runs with this process's stripped environment.
    """
    del timeout_s
    explicit = bool((env.get(CLAUDE_CLI_BIN_ENV) or "").strip())
    model = (env.get(CLAUDE_CLI_MODEL_ENV) or "").strip() or DEFAULT_CLAUDE_CLI_MODEL
    binary = resolve_claude_cli(env)
    if binary is None:
        why = f"{CLAUDE_CLI_BIN_ENV} not executable" if explicit else "not on PATH"
        return {
            "leg": "claude_cli",
            "configured": False,
            "explicit": False,
            "reachable": None,
            "detail": f"{CLAUDE_CLI_MISSING_REASON} ({why})",
        }
    entry: dict[str, Any] = {
        "leg": "claude_cli",
        "configured": True,
        "explicit": explicit or bool((env.get(CLAUDE_CLI_MODEL_ENV) or "").strip()),
    }
    base = _claude_cli_run_base(env)
    if base is None:
        return {**entry, "reachable": False, "detail": f"{model}: run dir unusable"}
    caps = _claude_cli_capabilities(binary, base, env)
    if not caps.get("ok"):
        why = caps.get("why", "unknown")
        return {
            **entry,
            "reachable": False,
            "detail": f"{model}: {CLAUDE_CLI_UNSUPPORTED_REASON} ({why})",
        }
    facts = _claude_cli_auth(binary, base, env)
    if facts.get("ok"):
        detail = (
            f"{model}: authMethod={facts['auth_method']}, "
            f"subscriptionType={facts['subscription_type']}"
        )
        return {**entry, "reachable": True, "detail": detail, "caveat": CLAUDE_CLI_LOGIN_CAVEAT}
    why = facts.get("why", "unknown")
    return {
        **entry,
        "reachable": False,
        "detail": f"{model}: {CLAUDE_CLI_NOT_SUBSCRIPTION_REASON} ({why})",
    }


def leg_status(
    env: Mapping[str, str] | None = None,
    *,
    timeout_s: float = LEG_STATUS_TIMEOUT_S,
    memory_agent: bool = False,
) -> list[dict]:
    """Per-leg configured/reachable status for the fallback chain — no spend.

    Exists for the doctor-style health read (task #1178): until then the only
    way to learn whether a leg worked was to run `complete()`, which spends
    the moment a leg answers. Legs are listed in chain order, then the opt-in
    subscription pool. Every probe here is unbilled: claude_cli runs only
    `claude --help` and `claude auth status --json` (task #2025: no
    completion), OpenRouter gets
    its `GET /api/v1/auth/key` account-metadata endpoint and then the public
    `GET /api/v1/models` listing, which must contain the configured model id
    exactly, and the subscription pool gets `GET {url}/models` — never a
    generation call. A leg with no config is not probed at all: checking a
    credentials endpoint without the credential proves nothing. ollama and
    ai_studio are not reported: neither is a chain leg any more
    (`_ollama_leg_status` remains for an explicit check of a local model).

    `env` defaults to `os.environ`; doctor passes the hub unit's merged
    Environment(File) so the report describes the service, not the caller.

    Each entry is {leg, configured, explicit, reachable, detail}:
    `configured` is config presence, `explicit` distinguishes an operator
    opt-in from a default the leg found on its own (the difference between
    "nothing configured" and "configured but dead"), and `reachable` is None for an
    unprobed, unconfigured leg. Never raises.
    """
    env = os.environ if env is None else env
    selected, model = memory_model_settings(env) if memory_agent else (None, None)
    if selected:
        model = _memory_effective_model(selected, model, env)
        model_key = {
            "claude_cli": CLAUDE_CLI_MODEL_ENV,
            "openrouter": OPENROUTER_MODEL_ENV,
            "subscription_pool": SUBSCRIPTION_POOL_MODEL_ENV,
        }[selected]
        env = {**env, model_key: model}
    legs: list[dict] = []

    def add(leg, configured, explicit, reachable, detail, caveat=None):
        entry = {
            "leg": leg,
            "configured": configured,
            "explicit": explicit,
            "reachable": reachable,
            "detail": detail,
        }
        if caveat:
            entry["caveat"] = caveat
        legs.append(entry)

    if not selected or selected == "claude_cli":
        legs.append(_claude_cli_leg_status(env, timeout_s=timeout_s))

    if not selected or selected == "openrouter":
        or_key = env.get(OPENROUTER_KEY_ENV, "")
        if not or_key:
            add("openrouter", False, False, None, f"{OPENROUTER_KEY_ENV} unset")
        else:
            or_model = (env.get(OPENROUTER_MODEL_ENV) or "").strip() or DEFAULT_OPENROUTER_MODEL
            ok, why = _probe_endpoint(
                "https://openrouter.ai/api/v1/auth/key",
                {"Authorization": f"Bearer {or_key}"},
                timeout_s,
            )
            detail = f"auth/key: {why}"
            if ok:
                ok, why = _probe_endpoint(
                    "https://openrouter.ai/api/v1/models",
                    {},
                    timeout_s,
                    expect_model=or_model,
                    exact_model=True,
                )
                detail = f"{detail}; models: {why}"
            add("openrouter", True, True, ok, detail)

    if not selected or selected == "subscription_pool":
        if env.get(SUBSCRIPTION_POOL_ENABLED_ENV, "0") != "1":
            add(
                "subscription_pool",
                False,
                False,
                None,
                f"{SUBSCRIPTION_POOL_ENABLED_ENV} unset",
            )
        elif not env.get(SUBSCRIPTION_POOL_URL_ENV, "").strip():
            add(
                "subscription_pool",
                True,
                True,
                False,
                f"{SUBSCRIPTION_POOL_ENABLED_ENV}=1 but {SUBSCRIPTION_POOL_URL_ENV} unset",
            )
        else:
            pool_url = env[SUBSCRIPTION_POOL_URL_ENV].strip()
            pool_key = env.get(SUBSCRIPTION_POOL_API_KEY_ENV, "").strip()
            pool_headers = {"Authorization": f"Bearer {pool_key}"} if pool_key else {}
            ok, why = _probe_endpoint(
                f"{pool_url.rstrip('/')}/models",
                pool_headers,
                timeout_s,
                expect_model=(
                    env.get(SUBSCRIPTION_POOL_MODEL_ENV) or DEFAULT_SUBSCRIPTION_POOL_MODEL
                    if selected == "subscription_pool"
                    else None
                ),
                exact_model=selected == "subscription_pool",
            )
            add("subscription_pool", True, True, ok, f"{pool_url}: {why}")

    if selected:
        for leg in legs:
            leg["memory_selected"] = True
            leg["explicit"] = True
    return legs


def memory_model_settings(env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """Resolve memory-only routing without mutating shared provider settings.

    Unset provider/model preserve the legacy chain. An explicit provider pins
    one existing leg; model overrides its provider-specific default. Model-only
    configuration is ambiguous and refused. Values are resolved on every call.
    """
    source = os.environ if env is None else env
    provider = source.get(MEMORY_PROVIDER_ENV, "").strip()
    model = source.get(MEMORY_MODEL_ENV, "").strip()
    if provider and provider not in {"claude_cli", "openrouter", "subscription_pool"}:
        raise ValueError("UA_MEMORY_PROVIDER must be claude_cli, openrouter, or subscription_pool")
    if model and not provider:
        raise ValueError("UA_MEMORY_MODEL requires UA_MEMORY_PROVIDER")
    if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", model):
        raise ValueError("UA_MEMORY_MODEL must be a valid provider model identifier")
    return provider or None, model or None


def _memory_effective_model(provider: str, override: str | None, env=None) -> str:
    """One model resolution for pinned invocation, metadata probes and provenance."""
    source = os.environ if env is None else env
    key, default = {
        "claude_cli": (CLAUDE_CLI_MODEL_ENV, DEFAULT_CLAUDE_CLI_MODEL),
        "openrouter": (OPENROUTER_MODEL_ENV, DEFAULT_OPENROUTER_MODEL),
        "subscription_pool": (SUBSCRIPTION_POOL_MODEL_ENV, DEFAULT_SUBSCRIPTION_POOL_MODEL),
    }[provider]
    # A blank setting means the default, as for the ordinary adapters (#2034).
    model = (override or "").strip() or (source.get(key) or "").strip() or default
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", model):
        raise ValueError("selected memory provider requires a valid model identifier")
    return model


def complete(
    prompt: str,
    system: str | None = None,
    *,
    response_schema: dict[str, Any] | None = None,
    response_mime_type: str | None = None,
    subscription_pool: bool = False,
    memory_agent: bool = False,
) -> dict:
    """Try each provider in the fallback chain in order; return the first success.

    Returns {text, provider, model, route}. `route` is the Phase 1 per-call
    metadata normalization (proposal item 4): the ordered list of
    {target, ok, elapsed_s} records for every leg attempted, so a caller or
    log line can see what was tried and how long each leg took without
    re-deriving it from timestamps. A failed leg's record also carries
    `error` (an exception class or a category such as Unconfigured or
    SchemaMismatch), `reason` (one entry of LEG_REASONS) and, for an HTTP
    failure, `status` — never provider text (task #1639). Raises LLMConnectionError if every
    provider in the chain returned None (unconfigured or unreachable) —
    callers should let this propagate to a non-zero process exit rather than
    swallow it (design doc D3).

    When `response_schema` is set, every leg is told the schema in its system
    prompt (OpenRouter additionally uses JSON mode, and the explicit-only
    Ollama helper constrained decoding) and returns None when the reply does not satisfy it, so the chain
    falls through cleanly.
    """
    # Memory-only overrides pin an existing leg and fail closed; other callers
    # retain the legacy chain and provider-specific model configuration.
    route: list[dict] = []
    chain = _PROVIDER_CHAIN
    if subscription_pool and _subscription_pool_enabled():
        # Inserted immediately before openrouter (task #1154): zero-spend
        # subscription capacity gets a turn ahead of the OpenRouter key, but
        # never ahead of the claude_cli subscription leg. Position is by
        # name, not a fixed index, so reordering _PROVIDER_CHAIN elsewhere
        # can't silently misplace this leg.
        insert_at = next(
            (i for i, (name, _) in enumerate(chain) if name == "openrouter"), len(chain)
        )
        chain = [
            *chain[:insert_at],
            ("subscription_pool", _call_subscription_pool),
            *chain[insert_at:],
        ]
    memory_provider, memory_model = memory_model_settings() if memory_agent else (None, None)
    if memory_provider:
        memory_model = _memory_effective_model(memory_provider, memory_model)
        providers = dict(_PROVIDER_CHAIN)
        # Retain both existing subscription opt-ins; a pinned disabled leg
        # records Unconfigured and never silently tries a different model.
        if subscription_pool:
            providers["subscription_pool"] = _call_subscription_pool
        if memory_provider not in providers:
            raise ValueError("memory provider requires the caller's subscription-pool opt-in")
        chain = [(memory_provider, providers[memory_provider])]
    for name, fn in chain:
        start = time.monotonic()
        token = _LEG_FAILURE.set(None)
        try:
            model_kwargs = {"model_override": memory_model} if memory_model else {}
            result = fn(
                prompt,
                system,
                response_schema=response_schema,
                response_mime_type=response_mime_type,
                **model_kwargs,
            )
            failure = _LEG_FAILURE.get()
        finally:
            _LEG_FAILURE.reset(token)
        elapsed_s = time.monotonic() - start
        attempt: dict[str, Any] = {
            "target": name,
            "ok": result is not None,
            "elapsed_s": elapsed_s,
        }
        if memory_model:
            attempt["model"] = memory_model
        if result is None:
            # task #1639: say why, not just that — as fixed facts only (see
            # _LEG_FAILURE). A leg that returned None without noting a reason
            # still gets a non-empty, honest record.
            attempt.update(failure or {"error": "Unavailable", "reason": "no result"})
        route.append(attempt)
        if result is not None:
            return {**result, "route": route}
    tried = [attempt["target"] for attempt in route]
    raise LLMConnectionError(
        f"all LLM providers unavailable or unreachable: {', '.join(tried)}", route=route
    )
