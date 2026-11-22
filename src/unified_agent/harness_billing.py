"""Which bill a native harness CLI charges: subscription or API (task #1837).

A run's ``run_usage.billing`` decides whether its cost counts as API spend.
Without it, a Claude Code run on a Max login that prints ``total_cost_usd``
is charged to a dollar budget at API prices, and a Codex run on a ChatGPT
login stays unknown-cost forever. Each CLI reports how it is authenticated
without printing a credential, so the answer is observed, never assumed:

* ``claude auth status`` prints JSON whose ``authMethod`` is ``claude.ai``
  for a subscription login, ``api_key`` when ``ANTHROPIC_API_KEY`` is set,
  and ``third_party`` for Bedrock, Vertex or Foundry (cloud API spend).
* ``codex login status`` prints, on stderr, ``Logged in using ChatGPT`` or
  ``Logged in using an API key`` (followed by a masked key). It names the
  login even when the session bills another provider (a ``-c model_provider``
  override, a profile, a system or project config layer, or a provider key
  such as ``CODEX_API_KEY``, ``OPENAI_API_KEY`` or ``OPENROUTER_API_KEY``).
  So a ChatGPT answer counts as ``subscription`` only with the measured
  session's own evidence: its rollout's ``session_meta.model_provider`` must
  be ``openai``. Without that evidence, or with a provider key in the child
  environment, the bill is unknown.

The probe runs with the environment and working directory the harness itself
gets, so it answers for that child, not for the caller, and it runs once per run: a cached answer could
keep exempting spend after the login or provider changed. Any other output, a
missing binary, a timeout or a non-zero exit is ``None``: an unknown bill stays
unknown. Nothing the probe prints is logged or returned.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

PROBE_TIMEOUT_S = 10.0

_CLAUDE_AUTH_METHODS = {"claude.ai": "subscription", "api_key": "api", "third_party": "api"}
_CODEX_STATUS = (
    ("logged in using chatgpt", "subscription"),
    ("logged in using an api key", "api"),
)
# Keys a Codex child can bill instead of the login ``codex login status`` names.
_CODEX_PROVIDER_KEYS = ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
# session_meta is a rollout's first record; a few lines of slack.
_SESSION_META_LINES = 5


def _run(argv: list[str], env: Mapping[str, str], cwd: str | None) -> tuple[str, str] | None:
    """``(stdout, stderr)`` of a successful probe, else None."""
    try:
        proc = subprocess.run(
            argv,
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return (proc.stdout or "", proc.stderr or "") if proc.returncode == 0 else None


def claude_billing(stdout: str | None) -> str | None:
    """``billing`` from ``claude auth status`` output, or None."""
    try:
        status = json.loads(stdout or "")
    except ValueError:
        return None
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        return None
    method = status.get("authMethod")
    return _CLAUDE_AUTH_METHODS.get(method) if isinstance(method, str) else None


def codex_session_provider(session_id: str | None, *, codex_home: Path | None = None) -> str | None:
    """``model_provider`` from a Codex session's rollout ``session_meta``, or None.

    ``codex_home`` defaults to ``$CODEX_HOME`` or ``~/.codex``. Only the first
    lines are read; the session store is the same one ``native_sessions`` uses.
    """
    from . import native_sessions

    if not session_id:
        return None
    path = native_sessions.session_file("codex", session_id, codex_dir=codex_home)
    if path is None:
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for _, line in zip(range(_SESSION_META_LINES), stream, strict=False):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("type") == "session_meta":
                    payload = record.get("payload")
                    provider = payload.get("model_provider") if isinstance(payload, dict) else None
                    return provider if isinstance(provider, str) else None
    except OSError:
        return None
    return None


def codex_billing(
    output: tuple[str, str] | None,
    env: Mapping[str, str],
    session_provider: str | None = None,
) -> str | None:
    """``billing`` from ``codex login status`` output, or None.

    ``session_provider`` is the measured session's ``model_provider``; a
    ChatGPT login counts as ``subscription`` only when it is ``openai``.
    """
    if output is None or any(env.get(name) for name in _CODEX_PROVIDER_KEYS):
        return None
    stdout, stderr = output
    lines = (stderr or stdout).strip().splitlines()
    status = lines[0].strip().lower() if lines else ""
    billing = next((b for prefix, b in _CODEX_STATUS if status.startswith(prefix)), None)
    if billing == "subscription" and session_provider != "openai":
        return None
    return billing


def probe(
    harness: str,
    *,
    binary: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    session_provider: str | None = None,
) -> str | None:
    """``subscription`` | ``api`` for one harness CLI under ``env``, else None.

    ``harness`` is ``claude`` or ``codex``; anything else is None. ``cwd`` is
    the child's working directory, whose project settings can select another
    credential. For Codex,
    ``session_provider`` is the measured session's evidence
    (:func:`codex_session_provider`).
    """
    if harness not in ("claude", "codex"):
        return None
    env = os.environ if env is None else env
    binary = binary or harness
    if harness == "claude":
        output = _run([binary, "auth", "status"], env, cwd)
        return claude_billing(output[0] if output else None)
    return codex_billing(_run([binary, "login", "status"], env, cwd), env, session_provider)
