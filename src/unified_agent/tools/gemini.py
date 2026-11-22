"""Google AI Studio (Gemini API key) text generation — a standalone tool for
burning AI Studio credits directly. Also backs llm_client's `ai_studio` leg,
which is now the only Gemini path: the ADC-based Vertex leg was removed in
task #197 because GCP Agent20 credit does not cover AI billing."""

import os
import shutil
import subprocess
from typing import Any

from google import genai
from google.genai import errors, types
from mcp.server.mcpserver.exceptions import ToolError

from .. import config as ua_config
from .. import usage_ledger

API_KEY_ENV = "GEMINI_API_KEY"
MODEL_ENV = "UA_GEMINI_MODEL"
DEFAULT_MODEL = "gemini-flash-latest"
REQUEST_TIMEOUT_S = 30.0


def gemini_ask(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    response_schema: Any | None = None,
    response_mime_type: str | None = None,
    thinking_budget: int | None = None,
) -> dict:
    """Ask Gemini via Google AI Studio API key using the official google-genai SDK.

    Auth: GEMINI_API_KEY, an API key from aistudio.google.com/apikey, sent as
    header authentication by the SDK client. Model defaults to UA_GEMINI_MODEL
    or gemini-flash-latest.

    Returns {text, model, usage: {prompt_tokens, output_tokens}}.
    """
    if not prompt or not prompt.strip():
        raise ToolError("prompt must not be empty")

    key = os.environ.get(API_KEY_ENV, "").strip()
    from .. import execution_locality

    # Task #2109: a devices-only hub never runs the agy CLI; use the API.
    prefer_agy = (
        os.environ.get("UA_PREFER_AGY_CLI", "0") == "1" and not execution_locality.devices_only()
    )
    structured_request = response_schema is not None or response_mime_type is not None

    if prefer_agy and shutil.which("agy") and not structured_request:
        full_prompt = f"System Instruction: {system}\n\n{prompt}" if system else prompt
        cmd = ["agy", "-p", full_prompt]
        use_model = (model or os.environ.get(MODEL_ENV, "")).strip()
        if use_model and use_model != DEFAULT_MODEL:
            cmd.extend(["--model", use_model])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=REQUEST_TIMEOUT_S)
            if res.returncode == 0 and res.stdout.strip():
                return {
                    "text": res.stdout.strip(),
                    "model": use_model or "agy-cli",
                    "usage": {"prompt_tokens": 0, "output_tokens": 0},
                }
        except Exception:
            pass

    if not key:
        raise ToolError(f"Google AI Studio provider needs {API_KEY_ENV} set")
    use_model = (model or os.environ.get(MODEL_ENV, "") or DEFAULT_MODEL).strip()

    # Snapshot actor/run before the call, not after it returns (task #1526
    # follow-up, Devin review of PR #875): a task can finish, or the agent can
    # pick up a new claim, while this request is in flight, and a post-hoc
    # lookup would attribute the call to the wrong run (or none).
    usage_actor = ua_config.verified_agent_identity()
    usage_run_id = usage_ledger.resolve_run_id(usage_actor)

    client = genai.Client(api_key=key, http_options={"timeout": int(REQUEST_TIMEOUT_S * 1000)})

    config_kwargs: dict[str, Any] = {}
    if system:
        config_kwargs["system_instruction"] = system
    if response_schema is not None:
        config_kwargs["response_schema"] = response_schema
        config_kwargs["response_mime_type"] = response_mime_type or "application/json"
    elif response_mime_type is not None:
        config_kwargs["response_mime_type"] = response_mime_type

    if thinking_budget is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)

    config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

    try:
        resp = client.models.generate_content(
            model=use_model,
            contents=prompt,
            config=config,
        )
    except errors.APIError as e:
        msg = str(e.message if hasattr(e, "message") and e.message else e)
        if key in msg:
            msg = msg.replace(key, "[REDACTED]")
        code = getattr(e, "code", "")
        raise ToolError(f"Gemini request failed: {code} {msg}".strip()) from e
    except Exception as e:
        msg = str(e)
        if key in msg:
            msg = msg.replace(key, "[REDACTED]")
        raise ToolError(f"Gemini request failed: {type(e).__name__}: {msg}") from e

    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        raise ToolError(f"Gemini returned no candidates: {resp}")

    first_cand = candidates[0]
    finish_reason = getattr(first_cand, "finish_reason", "unknown")
    text = resp.text or ""
    if not text and finish_reason and finish_reason != "STOP":
        raise ToolError(f"Gemini returned empty content (finishReason={finish_reason})")

    usage_meta = getattr(resp, "usage_metadata", None)
    prompt_tokens = getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0
    output_tokens = getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0

    # Hub-observed usage (task #1526): recorded here, once, for every caller —
    # the standalone `gemini_ask` tool and llm_client's ai_studio leg both go
    # through this function, so recording lives here rather than in each
    # caller to avoid a double-counted row. The AI Studio key path never
    # returns a price, so cost is always None, never a fabricated 0. Missing
    # usage_metadata records None token counts, not the tool's own 0 fallback
    # above (that 0 is this tool's return contract, not an observed fact).
    usage_ledger.record_observed(
        provider="ai_studio",
        model=use_model,
        input_tokens=usage_ledger.token_count(
            getattr(usage_meta, "prompt_token_count", None) if usage_meta else None
        ),
        output_tokens=usage_ledger.token_count(
            getattr(usage_meta, "candidates_token_count", None) if usage_meta else None
        ),
        cost_usd=None,
        actor=usage_actor,
        run_id=usage_run_id,
    )

    return {
        "text": text,
        "model": use_model,
        "usage": {
            "prompt_tokens": prompt_tokens or 0,
            "output_tokens": output_tokens or 0,
        },
    }
