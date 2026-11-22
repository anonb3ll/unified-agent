"""Subscription-only Codex completion adapter for the shared LLM selector.

The selector supplies its bounded process runner. This module neither launches
another agent lane nor owns a second subprocess implementation. ``run`` must
enforce the invocation's deadline through process exit and pipe EOF, cap both
streams, and kill/reap descendants on timeout or cancellation. Wiring that
runner and recording the returned observed usage are the selector's duties.

Only unpinned routing can use the native default. A pinned Haiku request is
excluded before authentication or generation. JSONL may omit the effective
model: ``requested_model`` then remains explicit and ``model`` is "unknown".

Shipped disabled (task #2026). A completion call must run with every agent tool
denied, and the Codex CLI offers no supported way to do that: ``codex exec`` has
no core-tool allow/deny switch and ``apply_patch`` cannot be turned off. The
read-only sandbox limits writes but does not prove a tool-free run. Until the CLI
supports a deny-all, ``CodexAdapter.complete`` refuses every request with
``DISABLED_REASON`` before it resolves a binary, creates a directory or calls its
runner. No environment variable or argument lifts the guard; re-enabling it is a
reviewed code change to ``TOOL_DENY_ALL_SUPPORTED`` once the CLI can prove a
tool-free run.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import jsonschema
from referencing import Registry

from .llm_client import reject_duplicate_keys
from .plan_usage_limit import is_plan_usage_limit

# Fail closed until the Codex CLI can run with all tools denied (task #2026).
# Deliberately a code constant, not configuration: flipping it needs review.
TOOL_DENY_ALL_SUPPORTED = False
DISABLED_REASON = "tool_deny_all_unsupported"

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TIMEOUT_S = 120.0
MAX_TIMEOUT_S = 300.0
AUTH_TIMEOUT_S = 15.0
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_EVENTS = 2048
MAX_CONTEXTS = 32
# ``--output-schema`` passes the file unchanged to the provider's strict
# structured-output format, which speaks one dialect. A schema that declares any
# other dialect is refused rather than checked under rules it did not ask for.
_SCHEMA_DIALECTS = frozenset(
    {
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }
)
_MODEL_RX = re.compile(r"(?:gpt-[A-Za-z0-9._-]+|o[1-9][A-Za-z0-9._-]*)\Z")
_CAPACITY_RX = re.compile(
    r"usage limit|rate limit|insufficient_quota|quota.exceeded|too many requests", re.I
)
_ENV_ALLOW = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "LANG",
        "TERM",
        "CODEX_HOME",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_COLLATE",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
    }
)


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    stdin: str | None = field(repr=False)
    env: Mapping[str, str] = field(repr=False)
    cwd: Path
    timeout_s: float
    max_stdout_bytes: int = MAX_OUTPUT_BYTES
    max_stderr_bytes: int = MAX_STDERR_BYTES


@dataclass(frozen=True)
class CLIResult:
    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)


class Runner(Protocol):
    def __call__(self, invocation: Invocation) -> CLIResult: ...


@dataclass(frozen=True)
class Completion:
    ok: bool
    requested_model: str
    provider: str = "codex_cli"
    model: str = "unknown"
    text: str | None = field(default=None, repr=False)
    error: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: None = None


def _failure(model: str, error: str) -> Completion:
    return Completion(False, model, error=error)


def child_env(source: Mapping[str, str], tmpdir: Path) -> dict[str, str]:
    result = {key: value for key, value in source.items() if key in _ENV_ALLOW}
    result["TMPDIR"] = str(tmpdir)
    return result


def _binary(env: Mapping[str, str]) -> str | None:
    explicit = env.get("UA_CODEX_CLI_BIN", "").strip()
    if explicit:
        if explicit.startswith("~/"):
            explicit = str(Path(env.get("HOME", "")) / explicit[2:])
        path = Path(os.path.abspath(explicit))
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which("codex", path=env.get("PATH", os.defpath))
    if found:
        return os.path.abspath(found)
    for candidate in (
        Path(env.get("HOME", "")) / ".local/bin/codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.absolute())
    return None


def _timeout(env: Mapping[str, str]) -> float:
    try:
        value = float(env.get("UA_CODEX_CLI_TIMEOUT_S", ""))
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return min(value, MAX_TIMEOUT_S) if math.isfinite(value) and value > 0 else DEFAULT_TIMEOUT_S


def _private_file(path: Path, text: str) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _exec_argv(
    binary: str, model: str, instruction_file: Path, schema_file: Path | None
) -> tuple[str, ...]:
    argv = [
        binary,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--json",
        "--color",
        "never",
        "--model",
        model,
    ]
    overrides = [
        'model_provider="openai"',
        'forced_login_method="chatgpt"',
        "features.shell_tool=false",
        "features.unified_exec=false",
        "features.shell_snapshot=false",
        "features.hooks=false",
        "features.plugins=false",
        "features.apps=false",
        "features.multi_agent=false",
        'web_search="disabled"',
        "tools.view_image=false",
        'approval_policy="never"',
        "mcp_servers={}",
        'history.persistence="none"',
        "model_instructions_file=" + json.dumps(str(instruction_file)),
    ]
    for override in overrides:
        argv.extend(("-c", override))
    if schema_file is not None:
        argv.extend(("--output-schema", str(schema_file)))
    argv.append("-")
    return tuple(argv)


def _dialect_supported(schema: object) -> bool:
    if not isinstance(schema, dict) or "$schema" not in schema:
        return True
    declared = schema["$schema"]
    return isinstance(declared, str) and declared in _SCHEMA_DIALECTS


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _total(values: list[int | None]) -> int | None:
    return sum(values) if values and all(v is not None for v in values) else None


def parse_result(
    result: CLIResult, requested_model: str, response_schema: dict | None = None
) -> Completion:
    if (
        len(result.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES
        or len(result.stderr.encode("utf-8")) > MAX_STDERR_BYTES
    ):
        return _failure(requested_model, "oversized_reply")
    messages: list[str] = []
    models: set[str] = set()
    inputs: list[int | None] = []
    caches: list[int | None] = []
    outputs: list[int | None] = []
    saw_completion = False
    error = None
    lines = result.stdout.splitlines()
    if len(lines) > MAX_EVENTS:
        return _failure(requested_model, "oversized_reply")
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return _failure(requested_model, "malformed_reply")
        if not isinstance(event, dict):
            return _failure(requested_model, "malformed_reply")
        kind = event.get("type")
        if kind in ("thread.started", "turn.started", "turn.completed"):
            model = event.get("model")
            if isinstance(model, str):
                if not _MODEL_RX.fullmatch(model):
                    return _failure(requested_model, "model_mismatch")
                models.add(model)
        if kind in ("turn.failed", "error"):
            detail = event.get("error", event.get("message", ""))
            if isinstance(detail, dict):
                detail = str(detail.get("code", "")) + " " + str(detail.get("message", ""))
            error = (
                "capacity"
                if isinstance(detail, str) and _CAPACITY_RX.search(detail)
                else "provider_error"
            )
        elif kind == "turn.completed":
            saw_completion = True
            usage = event.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            inputs.append(_count(usage.get("input_tokens")))
            caches.append(_count(usage.get("cached_input_tokens")))
            outputs.append(_count(usage.get("output_tokens")))
        elif kind in ("item.started", "item.completed"):
            item = event.get("item")
            if not isinstance(item, dict):
                return _failure(requested_model, "malformed_reply")
            if item.get("type") not in ("agent_message", "reasoning", "plan"):
                return _failure(requested_model, "unexpected_tool")
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    messages.append(text)
    if error or result.returncode or not saw_completion or not messages:
        # A usage ceiling may surface only as a stderr diagnostic, next to a
        # generic JSONL error or none at all. The text is bounded above and only
        # classified; it never leaves this function.
        if error != "capacity" and is_plan_usage_limit(result.stderr):
            error = "capacity"
        return _failure(requested_model, error or "provider_error")
    if models and models != {requested_model}:
        return _failure(requested_model, "model_mismatch")
    text = messages[-1]
    if response_schema is not None:
        if not _dialect_supported(response_schema):
            return _failure(requested_model, "unsupported_schema_dialect")
        try:
            validator = jsonschema.Draft202012Validator
            validator.check_schema(response_schema)
            # A duplicate key fails the leg (task #2089): last-key-wins would let
            # the consumer read a different value than the one validated here.
            reply = json.loads(text, object_pairs_hook=reject_duplicate_keys)
            validator(response_schema, registry=Registry()).validate(reply)
        except Exception:  # noqa: BLE001 - malformed schemas must never fetch remote references
            return _failure(requested_model, "schema_mismatch")
    # OpenAI's input_tokens already includes cached input; never add it twice.
    return Completion(
        True,
        requested_model,
        model=requested_model if models else "unknown",
        text=text,
        input_tokens=_total(inputs),
        cached_input_tokens=_total(caches),
        output_tokens=_total(outputs),
    )


class CodexAdapter:
    """One attempt per request; capacity failures receive a volatile backoff.

    The cooldown is an admission policy, not a claim that quota has reset.
    It is scoped to this adapter/account context and never writes credentials.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, cooldown_s: float = 300.0):
        self._clock = clock
        self._cooldown_s = min(max(cooldown_s, 1), 3600) if math.isfinite(cooldown_s) else 300.0
        self._blocked: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def complete(
        self,
        prompt: str,
        *,
        run: Runner,
        base_dir: Path,
        env: Mapping[str, str] | None = None,
        system: str | None = None,
        response_schema: dict | None = None,
        pinned_model: str | None = None,
    ) -> Completion:
        source = dict(os.environ if env is None else env)
        model = source.get("UA_CODEX_CLI_MODEL", "").strip() or DEFAULT_MODEL
        if not TOOL_DENY_ALL_SUPPORTED:
            return _failure(model, DISABLED_REASON)
        if pinned_model is not None and model != pinned_model:
            return _failure(model, "model_pin_excludes_codex")
        if not _MODEL_RX.fullmatch(model) or len(model) > 80:
            return _failure("invalid", "invalid_model")
        if response_schema is not None:
            if not _dialect_supported(response_schema):
                return _failure(model, "unsupported_schema_dialect")
            try:
                jsonschema.Draft202012Validator.check_schema(response_schema)
            except (jsonschema.SchemaError, RecursionError, ValueError):
                return _failure(model, "invalid_schema")
        context = (model, source.get("CODEX_HOME", source.get("HOME", "")))
        with self._lock:
            if self._blocked.get(context, 0) > self._clock():
                return _failure(model, "capacity_cooldown")
        binary = _binary(source)
        if binary is None:
            return _failure(model, "missing_binary")
        base = base_dir.resolve()
        if any((directory / ".git").exists() for directory in (base, *base.parents)):
            return _failure(model, "unsafe_workdir")
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="codex-", dir=base) as directory:
                root = Path(directory)
                cwd = root / "cwd"
                cwd.mkdir(mode=0o700)
                tmpdir = root / "tmp"
                tmpdir.mkdir(mode=0o700)
                child = child_env(source, tmpdir)
                auth = run(
                    Invocation(
                        (binary, "-c", 'forced_login_method="chatgpt"', "login", "status"),
                        None,
                        child,
                        cwd,
                        AUTH_TIMEOUT_S,
                    )
                )
                status_lines = [
                    line.strip().lower()
                    for line in (auth.stdout + "\n" + auth.stderr).splitlines()
                    if line.strip().lower().startswith("logged in using")
                ]
                if (
                    auth.returncode
                    or status_lines != ["logged in using chatgpt"]
                    or len(auth.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES
                    or len(auth.stderr.encode("utf-8")) > MAX_STDERR_BYTES
                ):
                    return _failure(model, "not_subscription")
                instruction_file = _private_file(
                    root / "instructions.txt",
                    system
                    or "Respond to the user request directly. Do not use tools, inspect files or execute commands.",
                )
                schema_file = (
                    _private_file(root / "schema.json", json.dumps(response_schema))
                    if response_schema is not None
                    else None
                )
                result = parse_result(
                    run(
                        Invocation(
                            _exec_argv(binary, model, instruction_file, schema_file),
                            prompt,
                            child,
                            cwd,
                            _timeout(source),
                        )
                    ),
                    model,
                    response_schema,
                )
        except subprocess.TimeoutExpired:
            return _failure(model, "timeout")
        except Exception:  # noqa: BLE001 - no command, prompt, or provider text escapes
            return _failure(model, "request_failed")
        if result.ok and pinned_model is not None and result.model != pinned_model:
            return _failure(model, "model_evidence_missing")
        if result.error == "capacity":
            with self._lock:
                now = self._clock()
                self._blocked.pop(context, None)
                if len(self._blocked) >= MAX_CONTEXTS:
                    for key in [k for k, until in self._blocked.items() if until <= now]:
                        del self._blocked[key]
                if len(self._blocked) >= MAX_CONTEXTS:
                    # Still full of active cooldowns: evict only the one ending soonest.
                    del self._blocked[min(self._blocked, key=self._blocked.__getitem__)]
                self._blocked[context] = now + self._cooldown_s
        return result
