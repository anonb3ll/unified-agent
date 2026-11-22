"""Task #1252 (Epic C, C4): subscription_pool opt-in at the named call sites.

The leg stays doubly gated — ``UA_SUBSCRIPTION_POOL_ENABLED=1`` in the
environment AND ``subscription_pool=True`` at the call site — so these tests
assert the *wiring contract*: the background research/planning callers named
by the runbook pass the flag, and the interactive chat callers (slack/zulip)
do not. The flag alone changes nothing at runtime without the env opt-in.
"""

from __future__ import annotations

import ast
from pathlib import Path

import unified_agent

_SRC = Path(unified_agent.__file__).resolve().parent


def _complete_calls(module: str) -> list[ast.Call]:
    """Every ``llm_client.complete(...)`` call node in a module's AST."""
    tree = ast.parse((_SRC / module).read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "complete"
            and isinstance(func.value, ast.Name)
            and func.value.id == "llm_client"
        ):
            out.append(node)
    return out


def _kwarg_names(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg}


def _subscription_pool_is_true(call: ast.Call) -> bool:
    for kw in call.keywords:
        if (
            kw.arg == "subscription_pool"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
        ):
            return True
    return False


def test_sleep_pass_digest_opts_in():
    calls = _complete_calls("sleep_pass.py")
    assert calls, "sleep_pass must call llm_client.complete"
    assert any(_subscription_pool_is_true(c) for c in calls), (
        "sleep-pass journal digest is a named zero-spend research call site"
    )


def test_consolidate_adjudication_opts_in():
    calls = _complete_calls("consolidate.py")
    assert calls, "consolidate must call llm_client.complete"
    assert any(_subscription_pool_is_true(c) for c in calls), (
        "cluster adjudication is a named zero-spend research call site"
    )


def test_session_capture_summarize_opts_in():
    calls = _complete_calls("session_capture.py")
    assert calls, "session_capture must call llm_client.complete"
    assert any(_subscription_pool_is_true(c) for c in calls), (
        "transcript summarization is a named zero-spend research call site"
    )


def test_interactive_chat_callers_do_not_opt_in():
    """slack_chat / zulip_chat are interactive user-facing replies — they keep
    the default chain and never ride the subscription proxy."""
    for module in ("slack_chat.py", "zulip_chat.py"):
        calls = _complete_calls(module)
        for call in calls:
            assert not _subscription_pool_is_true(call), (
                f"{module} must not pass subscription_pool=True"
            )


def test_per_call_flag_and_env_gate_still_required():
    """The two-gate contract survives: flag off by default, env still gates."""
    import inspect

    from unified_agent import llm_client

    sig = inspect.signature(llm_client.complete)
    assert sig.parameters["subscription_pool"].default is False
    import os

    os.environ.pop("UA_SUBSCRIPTION_POOL_ENABLED", None)
    assert llm_client._subscription_pool_enabled() is False
