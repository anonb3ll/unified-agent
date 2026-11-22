"""Per-harness ACP launch matrix for ua-node (task #1328, Epic I2).

One row per coding harness that can be driven over the Agent Client
Protocol: the argv that starts it as an ACP *agent* (JSON-RPC over stdio),
the env var that overrides the binary, and the binary probed on PATH so a
node only advertises ``acp:<harness>`` when the entrypoint really exists.

``devin``, ``agy`` and ``pi`` use executor.backends and have no ACP entry
in this matrix. An ``acp:`` name outside this table is refused, never guessed at.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from typing import Any

from . import session_settings

# harness name -> env override for the binary, argv tail after the binary,
# and the default binary probed on PATH.
ACP_HARNESSES: dict[str, dict[str, Any]] = {
    "gemini": {"env": "UA_GEMINI_BIN", "argv_tail": ["--acp"], "bin": "gemini"},
    "codex": {"env": "UA_CODEX_ACP_BIN", "argv_tail": [], "bin": "codex-acp"},
    "claude": {"env": "UA_CLAUDE_ACP_BIN", "argv_tail": [], "bin": "claude-agent-acp"},
    "copilot": {"env": "UA_COPILOT_BIN", "argv_tail": ["--acp"], "bin": "copilot"},
    "cursor": {"env": "UA_CURSOR_ACP_BIN", "argv_tail": ["acp"], "bin": "agent"},
    "opencode": {"env": "UA_OPENCODE_BIN", "argv_tail": ["acp"], "bin": "opencode"},
    "hermes": {"env": "UA_HERMES_BIN", "argv_tail": ["acp"], "bin": "hermes"},
}

ACP_PREFIX = "acp:"


def acp_backend_name(backend: str) -> str | None:
    """Map ``acp:<harness>`` -> harness name; None for non-ACP backends."""
    name = str(backend or "").strip().lower()
    if not name.startswith(ACP_PREFIX):
        return None
    return name[len(ACP_PREFIX) :]


HERMES_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HERMES_RESERVED_PROFILES = frozenset({"hermes", "test", "tmp", "root", "sudo"})


def validate_hermes_profile(profile: Any) -> str:
    """Native Hermes id grammar; never normalize a different requested identity."""
    if not isinstance(profile, str) or not HERMES_PROFILE_RE.fullmatch(profile):
        raise ValueError("hermes_profile must be an explicit lowercase profile id (1-64 chars)")
    if profile in _HERMES_RESERVED_PROFILES:
        raise ValueError("hermes_profile is reserved by native Hermes")
    return profile


def resolve_argv(harness: str, *, hermes_profile: str | None = None) -> list[str]:
    """argv that starts ``harness`` as an ACP agent; env override wins."""
    row = ACP_HARNESSES.get(harness)
    if row is None:
        raise ValueError(f"unknown acp harness {harness!r}")
    binary = (os.environ.get(row["env"]) if row["env"] else None) or row["bin"]
    if hermes_profile is not None:
        if harness != "hermes":
            raise ValueError("hermes_profile is only supported by acp:hermes")
        return [binary, "-p", validate_hermes_profile(hermes_profile), *row["argv_tail"]]
    return [binary, *row["argv_tail"]]


def acp_available(harness: str, *, which: Callable[[str], str | None] = shutil.which) -> bool:
    """True only when the harness's ACP entrypoint resolves (env path or PATH)."""
    try:
        binary = resolve_argv(harness)[0]
    except ValueError:
        return False
    if os.path.isabs(binary) or os.sep in binary:
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return which(binary) is not None


#: ua-node feature flag (registered facts ``node_features``) for operator
#: follow-ups; mirrored by ``node_followups.NODE_FEATURE``.
FOLLOWUP_FEATURE = "acp_followups"
#: ua-node feature flag for resume and adopt (task #1741): the node honours
#: ``resume_session_id`` on a claimed item. A node without it would ignore the
#: field and open a fresh session, so the hub refuses to queue a resume there.
RESUME_FEATURE = "session_resume"


def _followup_capabilities(
    acp: bool, advertised: bool, features: Any
) -> tuple[dict[str, str], dict[str, str]]:
    """The ``queue`` and ``steer`` rows (task #1740)."""
    if not acp:
        none = "Executor CLI runs one prompt per dispatch"
        return (
            {"state": "unsupported", "detail": none},
            {"state": "unsupported", "detail": none},
        )
    if not advertised:
        return (
            {"state": "unavailable", "detail": "Backend not advertised on this node"},
            {"state": "unavailable", "detail": "Backend not advertised on this node"},
        )
    if not (isinstance(features, (list, tuple, set, frozenset)) and FOLLOWUP_FEATURE in features):
        old = "This ua-node predates follow-ups; upgrade it"
        return ({"state": "unavailable", "detail": old}, {"state": "unavailable", "detail": old})
    return (
        {
            "state": "supported",
            "detail": "Delivered as the next session/prompt after the turn ends, "
            "one follow-up per turn, in the order the hub accepted them",
        },
        {
            # ACP v1 has no mid-turn input. The guaranteed path is emulated; an
            # agent advertising _session/steering at initialize (claude-agent-acp
            # does) gets it injected natively, and each delivery records its path.
            "state": "emulated",
            "detail": "session/cancel, then session/prompt with the new message; native "
            "_session/steering only when the agent advertises it at initialize",
        },
    )


#: Native CLI backends that continue a session with their own resume flag.
NATIVE_RESUME_FLAGS = {"claude": "claude -p --resume <id>", "codex": "codex exec resume <id>"}


def _resume_capability(
    backend: str, acp: bool, advertised: bool, features: Any, observed: str | None
) -> dict[str, str]:
    """The ``resume`` row (task #1741): only what this node and agent proved."""
    if not advertised:
        return {"state": "unavailable", "detail": "Backend not advertised on this node"}
    has_feature = isinstance(features, (list, tuple, set, frozenset)) and RESUME_FEATURE in features
    if not acp:
        flag = NATIVE_RESUME_FLAGS.get(backend)
        if flag is None:
            return {
                "state": "unsupported",
                "detail": "Start again opens a fresh session; this CLI has no resume path here",
            }
        if not has_feature:
            return {"state": "unavailable", "detail": "This ua-node predates resume; upgrade it"}
        return {
            "state": "supported",
            "detail": f"Adopted sessions continue with `{flag}`; hub-started CLI runs record "
            "no session id, so they Start again",
        }
    if not has_feature:
        return {"state": "unavailable", "detail": "This ua-node predates resume; upgrade it"}
    if observed == "resume":
        return {
            "state": "supported",
            "detail": "session/resume (advertised at initialize): reopens without a replay",
        }
    if observed == "load":
        return {
            "state": "supported",
            "detail": "session/load (advertised at initialize): history replays on the "
            "device and is not re-streamed",
        }
    if observed == "none":
        return {
            "state": "unsupported",
            "detail": "The agent advertised neither session/resume nor loadSession; "
            "Start again opens a fresh session",
        }
    return {
        "state": "unknown",
        "detail": "Recorded at the first session's initialize; until then Resume is not offered",
    }


def control_capabilities(
    backend: str,
    *,
    advertised: bool,
    version: str | None,
    features: Any = None,
    resume_mode: str | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Supported node contract, not a promise about unprobed native extensions.

    Actual ACP negotiation remains in session_open evidence. A binary on PATH
    alone cannot prove every extension (especially resume/steer/usage).
    ``features`` is the node's reported ``node_features`` list.
    ``resume_mode`` is what this backend's agent advertised at its latest
    initialize on this node (``resume`` | ``load`` | ``none``), or None when
    no session has recorded it yet.
    """
    acp = acp_backend_name(backend) in ACP_HARNESSES
    path = "ACP v1 over stdio" if acp else "executor CLI + receipt"
    supported = "supported" if advertised else "unavailable"
    queue, steer = _followup_capabilities(acp, advertised, features)
    capabilities = {
        "start": {
            "state": supported,
            "detail": "New session through the approval-gated node queue",
        },
        "prompt": {
            "state": supported,
            "detail": "Initial prompt, then operator follow-ups in the same session"
            if acp
            else "One initial prompt per dispatch",
        },
        "stream": {
            "state": supported if acp else "unsupported",
            "detail": "ACP updates through hub SSE"
            if acp
            else "Final receipt only; no live node event relay",
        },
        "resume": _resume_capability(backend, acp, advertised, features, resume_mode),
        "queue": queue,
        "steer": steer,
        "cancel": {
            "state": supported,
            "detail": "Heartbeat cancellation, then ACP cancel/process termination",
        },
        "permissions": {
            "state": supported if acp else "unsupported",
            "detail": "Native ACP requests relay to one-shot hub Approvals"
            if acp
            else "Launch approval only; CLI tool prompts are not relayed",
        },
        "usage": {
            "state": "reported" if acp and advertised else "unsupported",
            "detail": "Native usage updates/result when supplied; missing usage stays unknown"
            if acp
            else "No node usage contract",
        },
    }
    settings_feature = (
        isinstance(features, (list, tuple, set, frozenset)) and session_settings.FEATURE in features
    )
    for kind, key in (("modes", "mode"), ("model_switch", "model")):
        if not acp:
            state, detail = "unsupported", "CLI launch has no negotiated session settings here"
        elif not advertised or not settings_feature:
            state, detail = (
                "unavailable",
                "Node must advertise session_settings before selections are accepted",
            )
        elif key == "mode" and session_settings.native_default_supported(
            acp_backend_name(backend) or ""
        ):
            state, detail = (
                "unclassified",
                "Supervised request keeps native default with approval relay; native permission policy is unclassified; auto/full unavailable",
            )
        elif settings is None:
            state, detail = (
                "unknown",
                "Advertised at session setup; selections are checked before first prompt",
            )
        elif settings.get(f"{key}_path"):
            state, detail = "advertised", settings[f"{key}_path"] + "; revalidated on every session"
        else:
            state, detail = (
                "unsupported",
                "Agent did not advertise this setting; requested values are refused",
            )
        capabilities[kind] = {"state": state, "detail": detail}
    return {
        "advertised": advertised,
        "path": path,
        "source_version": version,
        "source": "acp_matrix.py/acp_backend.py" if acp else "executor/backends.py",
        "capabilities": capabilities,
    }
